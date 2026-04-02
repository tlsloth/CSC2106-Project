import json
import uasyncio as asyncio
import aioble
import bluetooth
import config
from utils import logger

TAG = "BLE_EDGE"

# Standard Mesh UUIDs
MESH_SERVICE_UUID = bluetooth.UUID(getattr(config, "BLE_MESH_SERVICE_UUID", 0x1111))
MESH_RX_CHAR_UUID = bluetooth.UUID(getattr(config, "BLE_MESH_RX_CHAR_UUID", 0x2222))

_dynamic_gateway = None 

# State tracking
_ble_active = False
_is_joined = False
_session_token = ""
_seq_num = 0
_fail_count = 0
MAX_FAILS = 3

# The Sensor's Mailbox (For receiving ACKs from the bridge)
_service = None
_rx_char = None


def init():
    """Initialise the Sensor's BLE GATT Server to receive ACKs."""
    global _ble_active, _service, _rx_char
    try:
        _service = aioble.Service(MESH_SERVICE_UUID)
        
        # write=True allows the bridge to drop ACKs here
        _rx_char = aioble.Characteristic(
            _service, 
            MESH_RX_CHAR_UUID, 
            write=True, 
            capture=True
        )
        
        aioble.register_services(_service)
        _ble_active = True
        logger.info(TAG, f"Edge BLE initialised.")
        return True
    except Exception as e:
        logger.error(TAG, f"BLE init failed: {e}")
        return False


# ==========================================
# 1. THE DELIVERY BOY (CENTRAL / TX)
# ==========================================
async def send_mesh_packet(pkt_dict):
    """Scan for mesh bridges, pick the closest one, and hand off the packet."""
    global _dynamic_gateway, _seq_num

    if not _ble_active:
        return False

    pkt_dict["token"] = _session_token
    pkt_dict["seq"] = _seq_num
    _seq_num += 1

    payload_str = json.dumps(pkt_dict)
    found_bridges = []
    
    try:
        logger.debug(TAG, f"Scanning for mesh networks to send {pkt_dict.get('type')}...")
        
        # 1. SCAN THE ROOM
        async with aioble.scan(duration_ms=3000, interval_us=30000, window_us=30000, active=True) as scanner:
            async for result in scanner:
                services = list(result.services())
                
                # Filter for our specific Mesh Network UUID
                if MESH_SERVICE_UUID not in services:
                    continue 
                
                device_name = result.name()
                if not device_name:
                    device_name = f"bridge_{result.device.addr_hex()[-4:]}"
                
                # If we are already joined, ONLY talk to our dynamic gateway to save time.
                if _is_joined and _dynamic_gateway and device_name != _dynamic_gateway:
                    continue
                    
                # Save the bridge and its signal strength
                found_bridges.append((result.rssi, device_name, result.device))

        if not found_bridges:
            logger.warn(TAG, "No mesh bridges found in range.")
            return False

        # 2. SORT BY SIGNAL STRENGTH (Strongest / Closest first)
        found_bridges.sort(key=lambda x: x[0], reverse=True)

        # 3. TRY TO CONNECT AND DELIVER
        for rssi, device_name, device in found_bridges:
            try:
                logger.debug(TAG, f"Attempting delivery to {device_name} (RSSI: {rssi})")
                connection = await device.connect(timeout_ms=2000)
                service = await connection.service(MESH_SERVICE_UUID)
                
                if service:
                    char = await service.characteristic(MESH_RX_CHAR_UUID)
                    if char:
                        await char.write(payload_str.encode('utf-8'))
                        logger.info(TAG, f"Packet '{pkt_dict.get('type')}' delivered to {device_name}")
                        await connection.disconnect()
                        
                        # IF THIS WAS A JOIN REQ, LOCK ONTO THIS BRIDGE!
                        if pkt_dict.get("type") == "join_req":
                            _dynamic_gateway = device_name
                            logger.info(TAG, f"Locked onto dynamic gateway: {_dynamic_gateway}")
                            
                        return True # Success!
                        
                await connection.disconnect()
            except Exception as e:
                logger.warn(TAG, f"Failed to connect to {device_name}, trying next... ({e})")
                
    except Exception as e:
        logger.error(TAG, f"BLE TX Scan Error: {e}")
        
    return False

# ==========================================
# 2. THE MAILBOX (PERIPHERAL / RX)
# ==========================================
async def rx_task():
    """Listen for ACKs and commands dropped into our mailbox by the Bridge."""
    global _is_joined, _session_token
    logger.info(TAG, "BLE Mailbox task started")
    
    while True:
        if not _ble_active:
            await asyncio.sleep(1)
            continue
            
        try:
            conn, data = await _rx_char.written()
            payload_str = data.decode('utf-8', 'ignore')
            msg = json.loads(payload_str)
            msg_type = msg.get("type", "")

            if msg_type == "join_ack":
                if msg.get("accepted"):
                    _session_token = msg.get("token", "")
                    _is_joined = True
                    logger.info(TAG, f"SUCCESS! Joined network via {msg.get('bridge_id')}. Token secured.")
                else:
                    logger.warn(TAG, f"Join rejected: {msg.get('reason')}")
            
            elif msg_type == "command":
                # Handle any dashboard commands routed to this sensor here
                logger.info(TAG, f"Received command from dashboard: {msg}")

        except Exception as e:
            logger.error(TAG, f"BLE RX Error: {e}")


# ==========================================
# 3. THE LIFECYCLE ROUTINES
# ==========================================
def _handle_tx_result(success):
    """Tracks consecutive failures and triggers a network rejoin if necessary."""
    global _fail_count, _is_joined, _dynamic_gateway
    
    if success:
        _fail_count = 0 # Reset counter on success
    else:
        _fail_count += 1
        logger.warn(TAG, f"Transmission failed. Strike {_fail_count}/{MAX_FAILS}")
        
        if _fail_count >= MAX_FAILS:
            logger.error(TAG, "Gateway lost! Purging session and initiating self-healing...")
            _is_joined = False
            _dynamic_gateway = None
            _fail_count = 0

async def join_network_task():
    """Attempt to join the mesh until successful."""
    global _is_joined
    import core.security as sec
    
    while True: # Changed to run forever, so it can catch disconnects
        if not _is_joined:
            join_req = {
                "kind": "control",
                "type": "join_req",
                "node_id": config.NODE_ID,
                "network": getattr(config, "MESH_NETWORK_NAME", "DEFAULT_MESH"),
                "auth": sec.generate_auth_hash(config.MESH_JOIN_KEY) 
            }
            
            logger.info(TAG, "Broadcasting Join Request to find a gateway...")
            await send_mesh_packet(join_req)
            
            # Wait 10 seconds for the Bridge to send the ACK before trying again
            await asyncio.sleep(10)
        else:
            # If joined, sleep and wait to see if we get disconnected
            await asyncio.sleep(1)

async def hello_task():
    """Periodically send a Hello to keep our route alive in the Bridge."""
    while True:
        if _is_joined:
            hello_pkt = {
                "kind": "control",
                "type": "hello",
                "node_id": config.NODE_ID
            }
            success = await send_mesh_packet(hello_pkt)
            _handle_tx_result(success) # Check if the gateway is still there!
            
        await asyncio.sleep(30)

async def telemetry_task(get_sensor_data_func):
    """Periodically read sensor data and push it to the Bridge."""
    while True:
        if _is_joined:
            sensor_data = get_sensor_data_func()
            
            data_pkt = {
                "kind": "data",
                "type": "sensor_data",
                "node_id": config.NODE_ID,
                "dst": "dashboard_main",
                "payload": sensor_data
            }
            
            logger.info(TAG, f"Sending telemetry: {sensor_data}")
            success = await send_mesh_packet(data_pkt)
            _handle_tx_result(success) # Check if the gateway is still there!
            
        await asyncio.sleep(15)
