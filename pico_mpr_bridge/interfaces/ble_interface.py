# interfaces/ble_interface.py — BLE central scan/read + Two-Way Mesh Server 

import json
import time
import config
from utils import logger
from core import packet
from core.neighbour import parse_hello
from core.security import check_node_token, check_join_auth, generate_join_token, xor_token_hex

_node_tokens = {}

TAG = "BLE"

_ble_active = False

# Ultrasonic Sensor UUIDs
_SERVICE_UUID = None
_CHAR_UUID = None

# Mesh Routing UUIDs and GATT objects
_MESH_SERVICE_UUID = None
_MESH_RX_CHAR_UUID = None
_mesh_service = None
_mesh_rx_char = None


def init():
    """Activate BLE, set up scanning UUIDs, and register the Mesh GATT Server."""
    global _ble_active, _SERVICE_UUID, _CHAR_UUID
    global _MESH_SERVICE_UUID, _MESH_RX_CHAR_UUID, _mesh_service, _mesh_rx_char
    
    try:
        import bluetooth
        import aioble

        ble = bluetooth.BLE()
        ble.active(True)

        # 1. Setup Legacy Sensor UUIDs
        if isinstance(config.BLE_SERVICE_UUID, str):
            _SERVICE_UUID = bluetooth.UUID(config.BLE_SERVICE_UUID)
        else:
            _SERVICE_UUID = bluetooth.UUID(config.BLE_SERVICE_UUID)
            
        if isinstance(config.BLE_CHAR_UUID, str):
            _CHAR_UUID = bluetooth.UUID(config.BLE_CHAR_UUID)
        else:
            _CHAR_UUID = bluetooth.UUID(config.BLE_CHAR_UUID)

        # 2. Setup Mesh Server UUIDs & Register Services
        _MESH_SERVICE_UUID = bluetooth.UUID(0x1111)
        _MESH_RX_CHAR_UUID = bluetooth.UUID(0x2222)

        _mesh_service = aioble.Service(_MESH_SERVICE_UUID)
        _mesh_rx_char = aioble.Characteristic(
            _mesh_service, _MESH_RX_CHAR_UUID, write=True, capture=True
        )
        aioble.register_services(_mesh_service)

        _ble_active = True
        logger.info(TAG, "BLE initialised, looking for: {}".format(
            getattr(config, 'BLE_DEVICE_NAME', 'any BLE device')))
        logger.debug(TAG, "Resolved UUIDs service={} characteristic={}".format(
            _SERVICE_UUID, _CHAR_UUID))
        return True
        
    except Exception as e:
        logger.error(TAG, "BLE init failed: {}".format(e))
        _ble_active = False
        return False


def is_available():
    return _ble_active



async def rx_task(ingress_queue, egress_queue, neighbour_table):
    """
    The Unified BLE Mailbox (Peripheral Server) with Security Interceptor.
    """
    import uasyncio as asyncio
    logger.info(TAG, "Unified BLE RX Server started")
    
    while True:
        if not _ble_active or not _mesh_rx_char:
            await asyncio.sleep(1)
            continue
            
        try:
            conn, value = await _mesh_rx_char.written()
            if value:
                payload = value.decode('utf-8', 'ignore')
                try:
                    msg = json.loads(payload)
                    msg_type = msg.get("type")
                    msg_src = msg.get("node_id") or msg.get("src") or "unknown"
                    
                    # --- 1. SECURITY INTERCEPTOR: Handle Join Requests ---
                    if msg_type == "join_req":
                        ok, reason = check_join_auth(
                            msg,
                            getattr(config, "MESH_NETWORK_NAME", ""),
                            getattr(config, "MESH_JOIN_KEY", "")
                        )
                        token = ""
                        if ok:
                            token = generate_join_token(token_bytes=8, entropy_hint=len(_node_tokens))
                            _node_tokens[msg_src] = token
                            neighbour_table.update(msg_src, protocols=["BLE"], capabilities=msg.get("capabilities", ["BLE"]))
                            logger.info(TAG, f"Join accepted for {msg_src}")
                        else:
                            _node_tokens.pop(msg_src, None)
                            logger.warn(TAG, f"Join rejected for {msg_src}: {reason}")
                            
                        # Generate and send the ACK directly back to the sensor
                        encrypted_token = xor_token_hex(token, getattr(config, "MESH_JOIN_KEY", "")) if token else ""
                        ack = {
                            "kind": "control",
                            "type": "join_ack",
                            "accepted": bool(ok),
                            "src": config.NODE_ID,
                            "bridge_id": config.NODE_ID,
                            "target_id": msg_src,
                            "reason": reason,
                            "token": encrypted_token,
                            "hop_dst": msg_src # CRITICAL: Tells tx_task to connect to the sensor!
                        }
                        egress_queue.push(5, ack)
                        continue # Consume the packet so main.py doesn't drop it!

                    # --- 2. SECURITY INTERCEPTOR: Validate Tokens ---
                    token_ok, reason = check_node_token(
                        msg, 
                        _node_tokens.get(msg_src), 
                        getattr(config, "MESH_JOIN_KEY", "")
                    )
                    
                    if not token_ok:
                        logger.warn(TAG, f"Dropped {msg_type} from {msg_src}: {reason}")
                        continue

                    # --- 3. NORMAL ROUTING (If authenticated) ---
                    neighbour_table.update(
                        msg_src,
                        protocols=["BLE"],
                        capabilities=msg.get("capabilities", ["BLE"])
                    )

                    msg["rssi"] = -50 
                    ingress_queue.push(msg.get("priority", 5), msg)
                    logger.debug(TAG, f"Received authenticated BLE packet from {msg_src}")
                    
                except ValueError:
                    logger.warn(TAG, "Received malformed JSON over BLE")
                    
        except Exception as e:
            logger.error(TAG, f"BLE RX Server Error: {e}")
            
        await asyncio.sleep_ms(50)

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

        payload_str = json.dumps(pkt)
        hop_dst = pkt.get("hop_dst")
        is_broadcast = (not hop_dst or pkt.get("type") in ["route_query", "hello"])

        try:
            async with aioble.scan(
                duration_ms=2500, interval_us=30000, window_us=30000, active=True
            ) as scanner:
                async for result in scanner:
                    device_name = result.name()
                    if not device_name:
                        continue

                    # ✅ FIX 1: Fixed typo 'adrr' → 'addr'
                    mac_addr = result.device.addr_hex()

                    # ✅ FIX 2: Filter by name prefix, not UUID (UUID unreliable on MicroPython)
                    is_mesh_node = (
                        device_name.startswith("bridge_") or
                        device_name.startswith("ble_sensor")
                    )
                    if not is_mesh_node:
                        continue

                    logger.debug(TAG, "Seen: {} ({})".format(device_name, mac_addr))

                    if not is_broadcast and device_name != hop_dst:
                        continue

                    try:
                        connection = await result.device.connect(timeout_ms=3000)
                        await asyncio.sleep_ms(200)

                        service = await connection.service(_MESH_SERVICE_UUID)
                        if service:
                            char = await service.characteristic(_MESH_RX_CHAR_UUID)
                            if char:
                                await char.write(payload_str.encode('utf-8'))
                                logger.info(TAG, "Transmitted {} to {}".format(
                                    pkt.get("type"), device_name))
                                if is_broadcast:
                                    neighbour_table.update(
                                        device_name, protocols=["BLE"], rssi=result.rssi)
                        else:
                            logger.warn(TAG, "Service not found on {}".format(device_name))

                        await connection.disconnect()

                        # If unicast and delivered, stop scanning
                        if not is_broadcast:
                            break

                    except Exception as e:
                        logger.warn(TAG, "TX to {} failed: {}".format(device_name, e))
                        try:
                            await connection.disconnect()
                        except Exception:
                            pass

        except Exception as e:
            logger.error(TAG, "BLE TX scan error: {}".format(e))


async def hello_task(neighbour_table=None):
    import uasyncio as asyncio
    import aioble
    logger.info(TAG, "BLE Advertising as '{}'".format(config.NODE_ID))

    while True:
        if not _ble_active or not _MESH_SERVICE_UUID:
            await asyncio.sleep(1)
            continue
        try:
            async with await aioble.advertise(
                250_000,
                name=config.NODE_ID,
                services=[_MESH_SERVICE_UUID],
                timeout_ms=1000        # ← key: stop after 1s, DON'T hold connection
            ) as connection:
                pass                   # ← DON'T await connection.disconnected()
        except Exception as e:
            logger.debug(TAG, "Adv: {}".format(e))
        await asyncio.sleep_ms(50)



