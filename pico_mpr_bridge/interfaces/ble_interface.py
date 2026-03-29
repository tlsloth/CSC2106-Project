# interfaces/ble_interface.py — BLE central scan/read tasks via aioble

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
_SERVICE_UUID = None
_CHAR_UUID = None


def init():
    """Activate BLE and set up UUIDs for scanning."""
    global _ble_active, _SERVICE_UUID, _CHAR_UUID
    try:
        import bluetooth
        import aioble

        ble = bluetooth.BLE()
        ble.active(True)

        # Parse UUIDs (can be strings or integers)
        if isinstance(config.BLE_SERVICE_UUID, str):
            _SERVICE_UUID = bluetooth.UUID(config.BLE_SERVICE_UUID)
        else:
            _SERVICE_UUID = bluetooth.UUID(config.BLE_SERVICE_UUID)
            
        if isinstance(config.BLE_CHAR_UUID, str):
            _CHAR_UUID = bluetooth.UUID(config.BLE_CHAR_UUID)
        else:
            _CHAR_UUID = bluetooth.UUID(config.BLE_CHAR_UUID)

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


async def rx_task(ingress_queue, neighbour_table):
    """Async task: scan for BLE sensor, connect, read distance, and enqueue."""
    import uasyncio as asyncio
    import struct
    discovery_delay = getattr(config, "BLE_DISCOVERY_DELAY_MS", 250)
    discovery_timeout = getattr(config, "BLE_DISCOVERY_TIMEOUT_MS", 4000)
    discovery_retries = getattr(config, "BLE_DISCOVERY_RETRIES", 3)

    logger.info(TAG, "BLE RX task started")
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
                                # IMPORTANT: Do not break early from async discovery iterators.
                                # Letting them run to completion avoids aioble "Discovery in progress".
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
                                # classify_priority() expects payload and returns an int priority
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
