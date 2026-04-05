import json
import uasyncio as asyncio
import aioble
import bluetooth
import config
from utils import logger

TAG = "BLE_EDGE"

MESH_SERVICE_UUID = bluetooth.UUID(getattr(config, "BLE_MESH_SERVICE_UUID", 0x1111))
MESH_RX_CHAR_UUID = bluetooth.UUID(getattr(config, "BLE_MESH_RX_CHAR_UUID", 0x2222))

_dynamic_gateway = None
_ble_active      = False
_is_joined       = False
_session_token   = ""
_seq_num         = 0
_fail_count      = 0
MAX_FAILS        = 3

_service  = None
_rx_char  = None


def init():
    global _ble_active, _service, _rx_char
    try:
        _service = aioble.Service(MESH_SERVICE_UUID)
        _rx_char = aioble.Characteristic(
            _service,
            MESH_RX_CHAR_UUID,
            write=True,
            capture=True
        )
        aioble.register_services(_service)
        _ble_active = True
        logger.info(TAG, "Edge BLE initialised.")
        return True
    except Exception as e:
        logger.error(TAG, "BLE init failed: {}".format(e))
        return False


# ==========================================
# ADVERTISE so bridge can write join_ack back
# ==========================================
async def mailbox_advertise_task():
    """
    Advertise our NODE_ID + MESH_SERVICE_UUID so the bridge can
    find us and write join_ack / commands back to our _rx_char.
    Pauses when radio is busy scanning/sending.
    """
    logger.info(TAG, "BLE Mailbox+Advertise started")

    while True:
        if not _ble_active:
            await asyncio.sleep(1)
            continue

        if _radio_busy:
            await asyncio.sleep_ms(100)
            continue

        try:
            async with await aioble.advertise(
                250_000,
                name=config.NODE_ID,
                services=[MESH_SERVICE_UUID],
            ) as connection:
                try:
                    # Wait up to 8s for bridge to write something
                    conn, data = await asyncio.wait_for_ms(
                        _rx_char.written(), 8000
                    )
                    if data:
                        _handle_incoming(data)
                except asyncio.TimeoutError:
                    pass  # nothing wrote, re-advertise

        except Exception as e:
            logger.debug(TAG, "Adv error: {}".format(e))

        await asyncio.sleep_ms(50)


def _handle_incoming(data):
    """Process any message written to our mailbox by the bridge."""
    global _is_joined, _session_token
    try:
        msg      = json.loads(data.decode('utf-8', 'ignore'))
        msg_type = msg.get("type", "")

        if msg_type == "join_ack":
            if msg.get("accepted"):
                _session_token = msg.get("token", "")
                _is_joined     = True
                logger.info(TAG, "SUCCESS! Joined via {}. Token secured.".format(
                    msg.get("bridge_id")))
            else:
                logger.warn(TAG, "Join rejected: {}".format(msg.get("reason")))

        elif msg_type == "command":
            logger.info(TAG, "Command from dashboard: {}".format(msg))

    except Exception as e:
        logger.error(TAG, "Mailbox parse error: {}".format(e))


# ==========================================
# RADIO BUSY FLAG — prevents scan/advert clash
# ==========================================
_radio_busy = False


# ==========================================
# DELIVERY — scan, find bridge, write packet
# ==========================================
async def send_mesh_packet(pkt_dict):
    """Scan for mesh bridges and deliver the packet to the best one."""
    global _dynamic_gateway, _seq_num, _radio_busy

    if not _ble_active:
        return False

    # Stamp common fields
    pkt_dict["src"]     = config.NODE_ID
    pkt_dict["node_id"] = config.NODE_ID
    pkt_dict["token"]   = _session_token
    pkt_dict["seq"]     = _seq_num
    _seq_num += 1

    if _dynamic_gateway:
        pkt_dict["hop_dst"] = _dynamic_gateway

    payload_str   = json.dumps(pkt_dict)
    found_bridges = []

    _radio_busy = True
    await asyncio.sleep_ms(300)   # let advertise task stop cleanly

    try:
        async with aioble.scan(
            duration_ms=3000, interval_us=30000, window_us=30000, active=True
        ) as scanner:
            async for result in scanner:
                services = list(result.services())
                if MESH_SERVICE_UUID not in services:
                    continue

                device_name = result.name()
                if not device_name:
                    continue  # skip unnamed devices

                # If already joined, only talk to our locked gateway
                if _is_joined and _dynamic_gateway and device_name != _dynamic_gateway:
                    continue

                found_bridges.append((result.rssi, device_name, result.device))

        if not found_bridges:
            logger.warn(TAG, "No mesh bridges found.")
            _radio_busy = False
            return False

        # Strongest signal first
        found_bridges.sort(key=lambda x: x[0], reverse=True)

        for rssi, device_name, device in found_bridges:
            for attempt in range(1, 4):
                try:
                    connection = await device.connect(timeout_ms=5000)
                    await asyncio.sleep_ms(300)

                    service = await connection.service(MESH_SERVICE_UUID)
                    if not service:
                        await connection.disconnect()
                        break

                    char = await service.characteristic(MESH_RX_CHAR_UUID)
                    if not char:
                        await connection.disconnect()
                        break

                    await char.write(payload_str.encode('utf-8'))
                    logger.info(TAG, "Delivered '{}' to {}".format(
                        pkt_dict.get("type"), device_name))
                    await connection.disconnect()

                    # Lock onto this bridge after first successful delivery
                    if pkt_dict.get("type") == "join_req" or not _dynamic_gateway:
                        _dynamic_gateway = device_name
                        logger.info(TAG, "Locked onto gateway: {}".format(_dynamic_gateway))

                    _radio_busy = False
                    return True

                except Exception as e:
                    logger.warn(TAG, "Attempt {}/3 to {} failed: {}".format(
                        attempt, device_name, e))
                    try:
                        await connection.disconnect()
                    except Exception:
                        pass
                    await asyncio.sleep_ms(500 * attempt)

    except Exception as e:
        logger.error(TAG, "BLE TX Scan Error: {}".format(e))

    _radio_busy = False
    return False


# ==========================================
# FAILURE TRACKING — self-healing rejoin
# ==========================================
def _handle_tx_result(success):
    global _fail_count, _is_joined, _dynamic_gateway
    if success:
        _fail_count = 0
    else:
        _fail_count += 1
        logger.warn(TAG, "TX failed. Strike {}/{}".format(_fail_count, MAX_FAILS))
        if _fail_count >= MAX_FAILS:
            logger.error(TAG, "Gateway lost! Purging session, will rejoin...")
            _is_joined       = False
            _dynamic_gateway = None
            _fail_count      = 0


# ==========================================
# LIFECYCLE TASKS
# ==========================================
async def join_network_task():
    """Send join_req until accepted, then monitor for disconnection."""
    while True:
        if not _is_joined:
            import core.security as sec
            join_req = {
                "kind":    "control",
                "type":    "join_req",
                "node_id": config.NODE_ID,
                "src":     config.NODE_ID,
                "network": getattr(config, "MESH_NETWORK_NAME", "DEFAULT_MESH"),
                "auth":    sec.generate_auth_hash(config.MESH_JOIN_KEY),
            }
            logger.info(TAG, "Broadcasting join_req...")
            await send_mesh_packet(join_req)
            await asyncio.sleep(10)
        else:
            await asyncio.sleep(1)


async def hello_task():
    """Keep our route alive in the bridge's neighbour table."""
    while True:
        if _is_joined:
            hello_pkt = {
                "kind":    "control",
                "type":    "hello",
                "node_id": config.NODE_ID,
                "src":     config.NODE_ID,
                "hop_dst": _dynamic_gateway,
            }
            success = await send_mesh_packet(hello_pkt)
            _handle_tx_result(success)
        await asyncio.sleep(30)


async def telemetry_task(get_sensor_data_func):
    """
    Read HC-SR04 data and push to bridge.
    get_sensor_data_func must return a dict e.g. {"distance": 42.5}
    """
    while True:
        if _is_joined:
            sensor_data = get_sensor_data_func()

            if sensor_data is None:
                logger.warn(TAG, "Sensor read failed, skipping telemetry")
                await asyncio.sleep(15)
                continue

            data_pkt = {
                "kind":    "data",
                "type":    "sensor_data",
                "node_id": config.NODE_ID,
                "src":     config.NODE_ID,
                "hop_dst": _dynamic_gateway,
                "dst":     getattr(config, "MESH_TARGET_DST", "dashboard_main"),
                "payload": sensor_data,
                "priority": 5,
            }
            logger.info(TAG, "Sending telemetry: {}".format(sensor_data))
            success = await send_mesh_packet(data_pkt)
            _handle_tx_result(success)

        await asyncio.sleep(15)


async def main(get_sensor_data_func):
    """Launch all sensor BLE tasks together."""
    if not init():
        logger.error(TAG, "BLE init failed, aborting")
        return

    await asyncio.gather(
        asyncio.create_task(mailbox_advertise_task()),
        asyncio.create_task(join_network_task()),
        asyncio.create_task(hello_task()),
        asyncio.create_task(telemetry_task(get_sensor_data_func)),
    )