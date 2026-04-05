# interfaces/ble_interface.py — BLE Mesh Bridge Interface
#
# Architecture (1-slot-safe):
#   Bridge is PERIPHERAL-ONLY for mesh comms:
#     - Advertises as "bridge_C" (or config.NODE_ID)
#     - Exposes RX char (0x2222): sensor/bridge writes packets here
#     - Exposes TX char (0x3333): bridge writes ack/response here, sensor reads it
#
#   hello_task advertises so sensors/bridges can find us.
#   rx_server_task reads _mesh_rx_char.written() — no radio needed.
#   tx_task (egress → outbound) is intentionally DISABLED here because the bridge
#     never needs to initiate a BLE central connection in this design.
#     Outbound mesh traffic routes via LoRa or WiFi-Direct.
#     If you need BLE-central egress, re-enable and it will work because
#     hello_task releases the lock before connect.
#
#   join_ack delivery: bridge writes ack into _mesh_tx_char immediately after
#     processing join_req. The sensor reads it in the same open connection.
#     No second outbound scan/connect needed → zero extra connection slots.

import json
import time
import config
import gc
from utils import logger
from core import packet
from core.neighbour import parse_hello
from core.security import check_node_token, check_join_auth, generate_join_token

_node_tokens = {}

TAG = "BLE"

_ble_active = False

# Legacy Sensor UUIDs (central scan for raw distance sensors)
_SERVICE_UUID = None
_CHAR_UUID    = None

# Mesh GATT objects
_MESH_SERVICE_UUID = None
_MESH_RX_CHAR_UUID = None   # 0x2222 — sensor/peer writes packets here
_MESH_TX_CHAR_UUID = None   # 0x3333 — bridge writes acks here, sensor reads
_mesh_service  = None
_mesh_rx_char  = None
_mesh_tx_char  = None       # NEW: outbound ack characteristic

# Radio mutex
_radio_lock = None

def _get_lock():
    global _radio_lock
    import uasyncio as asyncio
    if _radio_lock is None:
        _radio_lock = asyncio.Lock()
    return _radio_lock


# ──────────────────────────────────────────────
# Init
# ──────────────────────────────────────────────

def init():
    global _ble_active, _SERVICE_UUID, _CHAR_UUID
    global _MESH_SERVICE_UUID, _MESH_RX_CHAR_UUID, _MESH_TX_CHAR_UUID
    global _mesh_service, _mesh_rx_char, _mesh_tx_char

    try:
        import bluetooth
        import aioble

        ble = bluetooth.BLE()
        ble.active(True)

        # Legacy sensor UUIDs
        _SERVICE_UUID = bluetooth.UUID(config.BLE_SERVICE_UUID)
        _CHAR_UUID    = bluetooth.UUID(config.BLE_CHAR_UUID)

        # Mesh UUIDs
        _MESH_SERVICE_UUID = bluetooth.UUID(0x1111)
        _MESH_RX_CHAR_UUID = bluetooth.UUID(0x2222)
        _MESH_TX_CHAR_UUID = bluetooth.UUID(0x3333)

        # GATT server: one service, two characteristics
        _mesh_service = aioble.Service(_MESH_SERVICE_UUID)

        # RX: central writes here (sensor → bridge)
        _mesh_rx_char = aioble.Characteristic(
            _mesh_service, _MESH_RX_CHAR_UUID,
            write=True, capture=True
        )

        # TX: bridge writes here, central reads (bridge → sensor ack)
        # read=True + notify=True so the sensor can either poll-read or subscribe
        _mesh_tx_char = aioble.Characteristic(
            _mesh_service, _MESH_TX_CHAR_UUID,
            read=True, notify=True
        )

        aioble.register_services(_mesh_service)

        _ble_active = True
        logger.info(TAG, "BLE initialised (peripheral mesh server, node={})".format(
            config.NODE_ID))
        return True

    except Exception as e:
        logger.error(TAG, "BLE init failed: {}".format(e))
        _ble_active = False
        return False


def is_available():
    return _ble_active


# ──────────────────────────────────────────────
# 1. Legacy central scan (raw distance sensors)
# ──────────────────────────────────────────────

async def rx_task(ingress_queue, neighbour_table):
    import uasyncio as asyncio
    import aioble
    import struct

    discovery_delay   = getattr(config, "BLE_DISCOVERY_DELAY_MS", 250)
    discovery_timeout = getattr(config, "BLE_DISCOVERY_TIMEOUT_MS", 4000)
    discovery_retries = getattr(config, "BLE_DISCOVERY_RETRIES", 3)
    expected_device   = getattr(config, "BLE_DEVICE_NAME", None)

    logger.info(TAG, "BLE legacy RX task started")

    while True:
        if not _ble_active or _SERVICE_UUID is None or _CHAR_UUID is None:
            await asyncio.sleep(5)
            continue

        async with _get_lock():
            try:
                async with aioble.scan(
                    duration_ms=config.BLE_SCAN_DURATION,
                    interval_us=30000, window_us=30000,
                ) as scanner:
                    async for result in scanner:
                        device_name = result.name() or "unknown"
                        if expected_device and device_name != expected_device:
                            continue

                        rssi    = result.rssi
                        node_id = device_name if device_name != "unknown" else str(result.device)
                        neighbour_table.update(node_id, protocols=["BLE"],
                                               rssi=rssi, capabilities=["BLE"])

                        connection = None
                        try:
                            connection = await result.device.connect(
                                timeout_ms=config.BLE_CONN_TIMEOUT)
                            await asyncio.sleep_ms(discovery_delay)

                            service = char = None
                            for attempt in range(1, discovery_retries + 1):
                                async for svc in connection.services(
                                        _SERVICE_UUID, timeout_ms=discovery_timeout):
                                    if not service and svc and svc.uuid == _SERVICE_UUID:
                                        service = svc
                                if service:
                                    async for ch in service.characteristics(
                                            _CHAR_UUID, timeout_ms=discovery_timeout):
                                        if not char and ch and ch.uuid == _CHAR_UUID:
                                            char = ch
                                    if char:
                                        break
                                await asyncio.sleep_ms(250)

                            if not service or not char:
                                continue

                            data = await char.read(timeout_ms=discovery_timeout)
                            if data and len(data) >= 2:
                                distance_cm = struct.unpack("<H", data[:2])[0]
                                if distance_cm == 0xFFFF or distance_cm >= 400:
                                    continue
                                logger.info(TAG, "BLE RX {}cm from {}".format(
                                    distance_cm, device_name))

                                trusted = getattr(config, "BLE_TRUSTED_SENSORS", [])
                                if node_id not in _node_tokens and node_id not in trusted:
                                    logger.warn(TAG, "Untrusted BLE sensor {}".format(node_id))
                                    continue

                                pkt = packet.create_packet(
                                    src=node_id, dst="bridge",
                                    payload={"distance": distance_cm})
                                if pkt:
                                    pkt["priority"] = packet.classify_priority(
                                        pkt.get("payload", {}))
                                    ingress_queue.push(pkt.get("priority",
                                                               packet.PRIORITY_NORMAL), pkt)
                        except Exception as e:
                            logger.warn(TAG, "BLE legacy connect error {}: {}".format(
                                device_name, e))
                        finally:
                            if connection:
                                try:
                                    await connection.disconnect()
                                    gc.collect()
                                except Exception:
                                    pass

            except Exception as e:
                logger.error(TAG, "BLE legacy scan error: {}".format(e))

        await asyncio.sleep_ms(config.BLE_SCAN_INTERVAL)


# ──────────────────────────────────────────────
# 2. Mesh RX server (peripheral — no radio lock)
# ──────────────────────────────────────────────

async def rx_server_task(ingress_queue, neighbour_table):
    """
    Wait for writes on _mesh_rx_char (0x2222).
    For join_req: validate, generate token, write join_ack to _mesh_tx_char (0x3333).
    The connected sensor reads 0x3333 in the SAME open connection — no extra connect.
    """
    import uasyncio as asyncio
    logger.info(TAG, "BLE Mesh RX server started")

    while True:
        if not _ble_active or not _mesh_rx_char:
            await asyncio.sleep(1)
            continue

        try:
            conn, value = await _mesh_rx_char.written()
            if not value:
                continue

            try:
                msg = json.loads(value.decode("utf-8", "ignore"))
            except ValueError:
                logger.warn(TAG, "Malformed BLE JSON, dropping")
                continue

            msg_type = msg.get("type", "")
            msg_src  = msg.get("src") or msg.get("node_id", "unknown")

            # ── JOIN REQUEST ──
            if msg_type == "join_req":
                logger.info(TAG, "JOIN_REQ from {}  net={} auth={}".format(
                    msg_src, msg.get("network"), msg.get("auth")))

                ok, reason = check_join_auth(
                    msg,
                    getattr(config, "MESH_NETWORK_NAME", "DEFAULT_MESH"),
                    config.MESH_JOIN_KEY
                )
                logger.info(TAG, "  → {} ({})".format("ACCEPT" if ok else "REJECT", reason))

                token_str = ""
                if ok:
                    token_str             = generate_join_token(msg_src, config.MESH_JOIN_KEY)
                    _node_tokens[msg_src] = token_str
                    neighbour_table.update(msg_src, protocols=["BLE"],
                                           rssi=-60, capabilities=["BLE"])

                ack = {
                    "type":      "join_ack",
                    "kind":      "control",
                    "src":       config.NODE_ID,
                    "bridge_id": config.NODE_ID,
                    "dst":       msg_src,
                    "accepted":  ok,
                    "token":     token_str,
                    "reason":    reason,
                }

                # Write ack into TX char — sensor reads it from the same connection
                _write_tx_char(ack)

            # ── HELLO ──
            elif msg_type == "hello":
                valid, reason = check_node_token(
                    msg_src, msg.get("token", ""), _node_tokens)
                if not valid:
                    logger.warn(TAG, "hello from {} rejected: {}".format(msg_src, reason))
                    continue
                neighbour_table.update(msg_src, protocols=["BLE"], rssi=-60)
                logger.debug(TAG, "hello from {} OK".format(msg_src))

            # ── DATA / ALL OTHER ──
            else:
                trusted = getattr(config, "BLE_TRUSTED_SENSORS", [])
                if msg_src not in trusted:
                    valid, reason = check_node_token(
                        msg_src, msg.get("token", ""), _node_tokens)
                    if not valid:
                        logger.warn(TAG, "BLE packet from {} rejected: {}".format(
                            msg_src, reason))
                        continue

                ingress_queue.push(msg.get("priority", 5), msg)
                logger.debug(TAG, "BLE {} from {} → ingress".format(msg_type, msg_src))

        except Exception as e:
            logger.error(TAG, "BLE rx_server error: {}".format(e))

        await asyncio.sleep_ms(10)


def _write_tx_char(msg_dict):
    """Synchronously update the TX characteristic value (no connection needed)."""
    if _mesh_tx_char is None:
        return
    try:
        data = json.dumps(msg_dict).encode("utf-8")
        _mesh_tx_char.write(data, send_update=True)
        logger.debug(TAG, "TX char updated: {}".format(msg_dict.get("type")))
    except Exception as e:
        logger.warn(TAG, "_write_tx_char failed: {}".format(e))


# ──────────────────────────────────────────────
# 3. Egress TX task (bridge → BLE central peer)
#    Only runs when the bridge needs to push a packet
#    to a BLE-addressed neighbour (rare in this design).
# ──────────────────────────────────────────────

async def tx_task(egress_queue, neighbour_table):
    import uasyncio as asyncio
    import aioble
    logger.info(TAG, "BLE Mesh TX task started")

    while True:
        if not _ble_active or egress_queue.is_empty():
            await asyncio.sleep_ms(100)
            continue

        pkt = egress_queue.pop()
        if not pkt:
            continue

        payload_bytes = json.dumps(pkt).encode("utf-8")
        hop_dst       = pkt.get("hop_dst")
        is_broadcast  = (not hop_dst or pkt.get("type") in ("route_query", "hello"))

        async with _get_lock():
            target_addr = target_addr_type = target_name = None
            try:
                async with aioble.scan(
                    duration_ms=2000, interval_us=30000, window_us=30000, active=True
                ) as scanner:
                    async for result in scanner:
                        name = result.name()
                        if not name:
                            continue
                        if is_broadcast or name == hop_dst:
                            target_addr_type = result.device.addr_type
                            target_addr      = bytes(result.device.addr)
                            target_name      = name
                            break
            except Exception as e:
                logger.error(TAG, "BLE TX scan error: {}".format(e))
                continue

            if not target_addr:
                logger.debug(TAG, "BLE TX: {} not found in scan".format(hop_dst))
                continue

            connection = None
            try:
                gc.collect()
                dev        = aioble.Device(target_addr_type, target_addr)
                connection = await asyncio.wait_for_ms(dev.connect(), 5000)
                await asyncio.sleep_ms(300)

                service = await asyncio.wait_for_ms(
                    connection.service(_MESH_SERVICE_UUID), 3000)
                if service:
                    char = await asyncio.wait_for_ms(
                        service.characteristic(_MESH_RX_CHAR_UUID), 3000)
                    if char:
                        await char.write(payload_bytes)
                        logger.debug(TAG, "TX {} → {}".format(
                            pkt.get("type"), target_name))

                await connection.disconnect()
                gc.collect()

            except Exception as e:
                logger.warn(TAG, "BLE TX connect error: {}".format(e))
                if connection:
                    try:
                        await connection.disconnect()
                    except Exception:
                        pass
                gc.collect()


# ──────────────────────────────────────────────
# 4. Hello advertising (bridge is discoverable)
# ──────────────────────────────────────────────

async def hello_task():
    import uasyncio as asyncio
    import aioble

    logger.info(TAG, "BLE hello_task started (advertising as {})".format(config.NODE_ID))

    while True:
        if not _ble_active or not _MESH_SERVICE_UUID:
            await asyncio.sleep(1)
            continue

        try:
            # Lock the radio for advertising
            async with _get_lock():
                connection = await aioble.advertise(
                    250_000,
                    name=config.NODE_ID,
                    services=[_MESH_SERVICE_UUID],
                    connectable=True,
                    timeout_ms=5000 
                )

                if connection:
                    logger.debug(TAG, "Central connected")
                    
                    # STAY INSIDE THE LOCK while connected.
                    # This prevents the bridge from trying to advertise 
                    # while the sensor is doing discovery.
                    await connection.disconnected()
                    
                    # 🔥 NEW: After they disconnect, keep the lock for 1 second
                    # to let the BLE stack clean up before the next advertise.
                    await asyncio.sleep_ms(1000)
                    logger.debug(TAG, "Central disconnected & Radio settled")

        except Exception:
            pass

        # Small breather between advertising windows
        await asyncio.sleep_ms(500)
