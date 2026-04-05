# interfaces/ble_interface.py — BLE Mesh Bridge Interface

import json
import time
import config
import gc
from utils import logger
from core import packet
from core.neighbour import parse_hello
import bluetooth
ble2 = bluetooth.BLE()


_node_tokens = {}
TAG = "BLE"
_ble_active = False

_SERVICE_UUID      = None
_CHAR_UUID         = None
_MESH_SERVICE_UUID = None
_MESH_RX_CHAR_UUID = None
_MESH_TX_CHAR_UUID = None
_mesh_service      = None
_mesh_rx_char      = None
_mesh_tx_char      = None

# Raw BLE handle for RX char value — needed for gatts_write buffer expansion
_rx_value_handle   = None

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
    global _rx_value_handle

    try:
        import bluetooth
        import aioble

        ble = bluetooth.BLE()
        ble.active(True)

        # ── KEY FIX: Set MTU to 247 on the peripheral side ──
        # This allows the remote central to negotiate a larger MTU,
        # letting it send writes > 20 bytes without Error 13 (ENOMEM).
        ble.config(mtu=247)

        _SERVICE_UUID      = bluetooth.UUID(config.BLE_SERVICE_UUID)
        _CHAR_UUID         = bluetooth.UUID(config.BLE_CHAR_UUID)
        _MESH_SERVICE_UUID = bluetooth.UUID(0x1111)
        _MESH_RX_CHAR_UUID = bluetooth.UUID(0x2222)
        _MESH_TX_CHAR_UUID = bluetooth.UUID(0x3333)

        _mesh_service = aioble.Service(_MESH_SERVICE_UUID)

        # RX: central writes here (sensor → bridge)
        _mesh_rx_char = aioble.Characteristic(
            _mesh_service, _MESH_RX_CHAR_UUID,
            write=True, capture=True
        )

        # TX: bridge writes here, central reads (bridge → sensor ack)
        _mesh_tx_char = aioble.Characteristic(
            _mesh_service, _MESH_TX_CHAR_UUID,
            read=True, notify=True
        )

        aioble.register_services(_mesh_service)
        
        print("DEBUG RX handle:", _mesh_rx_char._value_handle)
        print("DEBUG TX handle:", _mesh_tx_char._value_handle)

        # ── KEY FIX: Expand RX characteristic buffer to 256 bytes ──
        # By default aioble caps characteristic writes at 20 bytes (ATT MTU - 3).
        # Pre-writing with bytes(256) raises the internal buffer ceiling so the
        # NimBLE stack can accept writes up to 256 bytes after MTU exchange.
        # This must be called AFTER register_services().
        try:
            # Access the raw handle aioble stored on the characteristic
            rx_handle = _mesh_rx_char._value_handle
            _rx_value_handle = rx_handle
            ble.gatts_write(rx_handle, bytes(256))
            logger.info(TAG, "RX char buffer expanded to 256 bytes (handle {})".format(rx_handle))
        except Exception as e:
            logger.warn(TAG, "gatts_write buffer expand failed: {} — writes may be truncated".format(e))

        _ble_active = True
        logger.info(TAG, "BLE initialised (peripheral mesh server, node={})".format(config.NODE_ID))
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
# 2. Mesh RX server (peripheral — no radio lock needed)
# ──────────────────────────────────────────────

async def rx_server_task(ingress_queue, neighbour_table):
    """
    Wait for writes on _mesh_rx_char (0x2222).
    For join_req: validate, write join_ack to _mesh_tx_char (0x3333).
    Sensor reads ack in the same open connection.
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

            logger.debug(TAG, "RX {} from {} ({} bytes)".format(msg_type, msg_src, len(value)))

            # ── JOIN REQUEST ──
            if msg_type == "join_req":
                logger.info(TAG, "JOIN_REQ from {} net={}".format(msg_src, msg.get("network")))

                # No auth check — accept all
                ok = True
                _node_tokens[msg_src] = "trusted"
                neighbour_table.update(msg_src, protocols=["BLE"], rssi=-60, capabilities=["BLE"])

                ack = {
                    "type":      "join_ack",
                    "kind":      "control",
                    "src":       config.NODE_ID,
                    "bridge_id": config.NODE_ID,
                    "dst":       msg_src,
                    "accepted":  True,
                    "token":     "",
                    "reason":    "ok",
                }
                _write_tx_char(ack)

            # ── HELLO ──
            elif msg_type == "hello":
                neighbour_table.update(msg_src, protocols=["BLE"], rssi=-60)
                logger.debug(TAG, "hello from {} OK".format(msg_src))

            # ── DATA / ALL OTHER ──
            else:
                ingress_queue.push(int(msg.get("priority", 5)), msg)
                logger.debug(TAG, "BLE {} from {} → ingress".format(msg_type, msg_src))

        except Exception as e:
            logger.error(TAG, "BLE rx_server error: {}".format(e))

        await asyncio.sleep_ms(10)


def _write_tx_char(msg_dict):
    """Synchronously update the TX characteristic value."""
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
                        logger.debug(TAG, "TX {} → {}".format(pkt.get("type"), target_name))

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
            logger.debug(TAG, "hello_task: advertising...")
            # No lock — bridge is peripheral-only, nothing to mutex
            async with await aioble.advertise(
                250_000,
                name=config.NODE_ID,
                services=[_MESH_SERVICE_UUID],
                connectable=True,
                timeout_ms=5000,
            ) as connection:
                logger.debug(TAG, "Central connected")
                await connection.disconnected()
                logger.debug(TAG, "Central disconnected")

        except Exception as e:
            logger.debug(TAG, "hello_task: adv window ended ({})".format(e))

        await asyncio.sleep_ms(100)
        
        
        
async def bridge_discovery_task(ingress_queue, neighbour_table):
    import uasyncio as asyncio
    import aioble
    logger.info(TAG, "BLE bridge discovery task started")

    while True:
        await asyncio.sleep(15)
        if not _ble_active:
            continue

        async with _get_lock():
            target_addr = target_addr_type = target_name = None
            try:
                async with aioble.scan(
                    duration_ms=4000, interval_us=30000, window_us=30000, active=True
                ) as scanner:
                    async for result in scanner:
                        name = result.name()
                        if not name or not name.startswith("bridge_"):
                            continue
                        if name == config.NODE_ID:
                            continue  # skip self
                        target_addr_type = result.device.addr_type
                        target_addr      = bytes(result.device.addr)
                        target_name      = name
                        logger.debug(TAG, "Found peer bridge: {} rssi={}".format(name, result.rssi))
                        break
            except Exception as e:
                logger.error(TAG, "Bridge discovery scan error: {}".format(e))
                continue

            if not target_addr:
                continue

            hello_pkt = {
                "type":    "hello",
                "kind":    "control",
                "src":     config.NODE_ID,
                "node_id": config.NODE_ID,
                "hop_dst": target_name,
                "token":   "",
                "seq":     0,
                "ttl":     3,
            }
            payload_bytes = json.dumps(hello_pkt).encode("utf-8")

            connection = None
            try:
                gc.collect()
                dev = aioble.Device(target_addr_type, target_addr)
                connection = await asyncio.wait_for_ms(dev.connect(), 5000)

                try:
                    await asyncio.wait_for_ms(connection.exchange_mtu(247), 3000)
                except Exception:
                    pass

                await asyncio.sleep_ms(500)

                # ── Collect services into list first ──
                services_found = []
                async for svc in connection.services():
                    services_found.append(svc)

                service = next(
                    (s for s in services_found if s.uuid == _MESH_SERVICE_UUID), None)

                if not service:
                    logger.warn(TAG, "Bridge discovery: mesh service not found on {}".format(
                        target_name))
                    await asyncio.wait_for_ms(connection.disconnect(), 3000)
                    gc.collect()
                    continue

                # ── Collect characteristics into list first ──
                chars_found = []
                async for c in service.characteristics():
                    chars_found.append(c)

                rx_char = next(
                    (c for c in chars_found if c.uuid == _MESH_RX_CHAR_UUID), None)

                if not rx_char:
                    logger.warn(TAG, "Bridge discovery: RX char not found on {}".format(
                        target_name))
                    await asyncio.wait_for_ms(connection.disconnect(), 3000)
                    gc.collect()
                    continue

                await asyncio.wait_for_ms(rx_char.write(payload_bytes, response=True), 5000)
                logger.info(TAG, "Sent hello to peer bridge {}".format(target_name))

                # Register peer bridge as neighbour locally
                neighbour_table.update(target_name, protocols=["BLE"],
                                       rssi=-60, capabilities=["BLE"])

                await asyncio.wait_for_ms(connection.disconnect(), 3000)
                gc.collect()

            except Exception as e:
                logger.warn(TAG, "Bridge discovery connect error: {}".format(e))
                if connection:
                    try:
                        await asyncio.wait_for_ms(connection.disconnect(), 3000)
                    except Exception:
                        pass
                gc.collect()



