# interfaces/ble_interface.py — BLE central scan/read + Two-Way Mesh Server

import json
import time
import config
from utils import logger
from core import packet
from core.neighbour import parse_hello
from core.security import check_node_token, check_join_auth, generate_join_token

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


# ==========================================
# 1. LEGACY SENSOR TASK (Central)
# ==========================================
async def rx_task(ingress_queue, neighbour_table):
    """Async task: scan for BLE sensor, connect, read distance, and enqueue."""
    import uasyncio as asyncio
    import struct
    discovery_delay = getattr(config, "BLE_DISCOVERY_DELAY_MS", 250)
    discovery_timeout = getattr(config, "BLE_DISCOVERY_TIMEOUT_MS", 4000)
    discovery_retries = getattr(config, "BLE_DISCOVERY_RETRIES", 3)

    logger.info(TAG, "BLE Sensor RX task started")
    expected_device = getattr(config, 'BLE_DEVICE_NAME', None)

    while True:
        if not _ble_active:
            await asyncio.sleep(5)
            continue

        if _SERVICE_UUID is None or _CHAR_UUID is None:
            logger.error(TAG, "BLE UUIDs not initialised; skipping scan cycle")
            await asyncio.sleep_ms(config.BLE_SCAN_INTERVAL)
            continue

        try:
            import aioble

            # Scan for devices advertising
            async with aioble.scan(
                duration_ms=config.BLE_SCAN_DURATION,
                interval_us=30000,
                window_us=30000,
            ) as scanner:
                async for result in scanner:
                    device_name = result.name() or "unknown"
                    rssi = result.rssi

                    # Only connect to the specific device we're looking for
                    if expected_device and device_name != expected_device:
                        continue
                    
                    logger.info(TAG, "Connecting to BLE device: {}".format(device_name))

                    # Update neighbour table
                    node_id = device_name if device_name != "unknown" else str(result.device)
                    neighbour_table.update(
                        node_id,
                        protocols=["BLE"],
                        rssi=rssi,
                        capabilities=["BLE"],
                    )

                    # Try to connect and read distance
                    try:
                        connection = await result.device.connect(
                            timeout_ms=config.BLE_CONN_TIMEOUT,
                        )
                        logger.debug(TAG, "Connected, discovering services...")
                        try:
                            # Give peripheral stack time to settle before discovery.
                            await asyncio.sleep_ms(discovery_delay)

                            service = None
                            char = None
                            for attempt in range(1, discovery_retries + 1):
                                async for svc in connection.services(_SERVICE_UUID, timeout_ms=discovery_timeout):
                                    if (service is None) and svc and svc.uuid == _SERVICE_UUID:
                                        service = svc

                                if service:
                                    logger.debug(TAG, "Found service on attempt {}".format(attempt))

                                    async for ch in service.characteristics(_CHAR_UUID, timeout_ms=discovery_timeout):
                                        if (char is None) and ch and ch.uuid == _CHAR_UUID:
                                            char = ch

                                    if char:
                                        logger.debug(TAG, "Found characteristic on attempt {}".format(attempt))
                                        break

                                logger.warn(TAG, "Discovery attempt {} failed for {}".format(attempt, device_name))
                                await asyncio.sleep_ms(250)

                            if not service:
                                logger.warn(TAG, "Service discovery failed for {}".format(device_name))
                                continue

                            if not char:
                                logger.warn(TAG, "Characteristic discovery failed for {}".format(device_name))
                                continue

                            logger.debug(TAG, "Found characteristic, reading...")
                            data = await char.read(timeout_ms=discovery_timeout)

                            if data and len(data) >= 2:
                                # Parse distance as little-endian unsigned short
                                distance_cm = struct.unpack("<H", data[:2])[0]
                                if distance_cm == 0xFFFF or distance_cm >= 400:
                                    logger.debug(TAG, "BLE distance unavailable/out-of-range from {}".format(device_name))
                                    continue
                                logger.info(TAG, "BLE RX distance: {} cm from {}".format(
                                    distance_cm, device_name))
                                
                            # BLE packet must come from a node that has joined the mesh
                            if node_id not in _node_tokens:
                                logger.warn(TAG, "BLE packet from {} with no join token; dropping".format(node_id))
                                continue
                            
                            # Create sensor packet
                            pkt = packet.create_packet(
                                src=node_id,
                                dst="bridge",
                                payload={"distance": distance_cm},
                            )
                            
                            # Mark neighbor as joined
                            neighbour_table.update(
                                node_id,
                                protocols=["BLE"],
                                rssi=rssi,
                                capabilities=["BLE"],
                            )
                            
                            if pkt and isinstance(pkt, dict):
                                pkt["priority"] = packet.classify_priority(pkt.get("payload", {}))
                                ingress_queue.push(pkt.get("priority", packet.PRIORITY_NORMAL), pkt)
                                logger.debug(TAG, "Enqueued BLE distance from {}".format(node_id))
                            else:
                                logger.warn(TAG, "create_packet returned invalid packet")
                            else:
                                logger.warn(TAG, "BLE read returned empty or invalid data")
                        finally:
                            await connection.disconnect()
                            logger.debug(TAG, "Disconnected from {}".format(device_name))
                    except Exception as e:
                        logger.warn(TAG, "BLE connect/read error for {}: {}".format(
                            device_name, str(e)))

        except Exception as e:
            logger.error(TAG, "BLE scan error: {}".format(e))

        # Wait before next scan cycle
        await asyncio.sleep_ms(config.BLE_SCAN_INTERVAL)


# ==========================================
# 2. TWO-WAY MESH TASKS (AODV Routing)
# ==========================================

async def rx_server_task(ingress_queue):
    """Act as a Peripheral: Listen for incoming mesh packets from other bridges."""
    import uasyncio as asyncio
    logger.info(TAG, "BLE Mesh Server started")
    
    while True:
        if not _ble_active or not _mesh_rx_char:
            await asyncio.sleep(1)
            continue
            
        try:
            # Wait for another bridge to write a packet to us
            conn, value = await _mesh_rx_char.written()
            if value:
                payload = value.decode('utf-8', 'ignore')
                try:
                    msg = json.loads(payload)
                    msg_src = msg.get("src") or msg.get("node_id", "unknown")
                    
                    # Instantly push to the router!
                    ingress_queue.push(msg.get("priority", 5), msg)
                    logger.debug(TAG, f"Received BLE mesh packet from {msg_src}")
                except ValueError:
                    logger.warn(TAG, "Received malformed JSON over BLE Mesh")
                    
        except Exception as e:
            logger.error(TAG, f"BLE Server Error: {e}")
            
        await asyncio.sleep_ms(50)


async def tx_task(egress_queue, neighbour_table):
    """Act as a Central: Connect and send packets to BLE neighbours."""
    import uasyncio as asyncio
    import aioble
    logger.info(TAG, "BLE Mesh TX task started")
    
    while True:
        if not _ble_active or egress_queue.is_empty():
            await asyncio.sleep_ms(100)
            continue
            
        pkt = egress_queue.pop()
        if not pkt: continue
        
        payload_str = json.dumps(pkt)
        hop_dst = pkt.get("hop_dst")
        
        # Determine if this is a broadcast (like a route_query)
        is_broadcast = (not hop_dst or pkt.get("type") in ["route_query", "hello"])

        try:
            # Quickly scan for the target bridge (or all bridges if broadcasting)
            async with aioble.scan(duration_ms=1500, interval_us=30000, window_us=30000) as scanner:
                async for result in scanner:
                    device_name = result.name()
                    if not device_name: continue
                    
                    if is_broadcast or device_name == hop_dst:
                        try:
                            # Connect, hand off the JSON packet, and immediately disconnect
                            connection = await result.device.connect(timeout_ms=2000)
                            service = await connection.service(_MESH_SERVICE_UUID)
                            if service:
                                char = await service.characteristic(_MESH_RX_CHAR_UUID)
                                if char:
                                    await char.write(payload_str.encode('utf-8'))
                                    logger.debug(TAG, f"Transmitted {pkt.get('type')} to {device_name}")
                                    
                                    # If it was a route_query broadcast, mark them in neighbour table!
                                    if is_broadcast:
                                        neighbour_table.update(device_name, protocols=["BLE"], rssi=result.rssi)
                                        
                            await connection.disconnect()
                        except Exception:
                            pass # Target might be busy, we'll try again next loop
        except Exception as e:
            logger.error(TAG, f"BLE TX Error: {e}")


async def hello_task():
    """Advertise our Node ID continuously so neighbours can find us."""
    import uasyncio as asyncio
    import aioble
    logger.info(TAG, "BLE Hello Advertising started")
    
    while True:
        if not _ble_active or not _MESH_SERVICE_UUID:
            await asyncio.sleep(1)
            continue
            
        try:
            # We literally broadcast our exact name (e.g., "bridge_C")
            async with await aioble.advertise(
                250_000, 
                name=config.NODE_ID, 
                services=[_MESH_SERVICE_UUID]
            ) as connection:
                # If someone connects to write to us, wait for them to finish and hang up
                await connection.disconnected()
        except Exception:
            pass
            
        await asyncio.sleep_ms(100)