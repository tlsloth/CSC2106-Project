# interfaces/wifi_direct_interface.py — Peer-to-Peer UDP Broadcast Mesh

import json
import time
import config
from utils import logger
from core import packet
from core.neighbour import create_hello_payload, parse_hello
import uasyncio as asyncio

try:
    import usocket as socket
except ImportError:
    import socket

TAG = "WIFI-DIR"
UDP_PORT = 5000
BROADCAST_IP = '255.255.255.255'

# Split into two separate sockets to solve lwIP routing issues
_sock_rx = None
_sock_tx = None
_udp_lock = asyncio.Lock()

def init():
    """Connect to WiFi and bind separate TX/RX UDP sockets."""
    global _sock_rx, _sock_tx, BROADCAST_IP
    try:
        import network
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        wlan.config(pm=0xa11140)

        # 1. Force WiFi Connection first!
        if not wlan.isconnected():
            logger.info(TAG, f"Connecting to WiFi: {config.WIFI_SSID}...")
            wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
            
            # Wait for connection
            timeout = 20
            while not wlan.isconnected() and timeout > 0:
                time.sleep(1)
                timeout -= 1
                
            if not wlan.isconnected():
                logger.error(TAG, "WiFi connection failed. Cannot start UDP.")
                return False
            
        # --- Calculate Subnet Broadcast IP ---
        ip, netmask, gw, dns = wlan.ifconfig()
        logger.info(TAG, f"WiFi Connected! IP: {ip}")
        
        #ip_parts = [int(x) for x in ip.split('.')]
        #nm_parts = [int(x) for x in netmask.split('.')]
        #bc_parts = [(ip_parts[i] | (~nm_parts[i] & 255)) for i in range(4)]
        #BROADCAST_IP = '.'.join([str(x) for x in bc_parts])
        logger.info(TAG, f"Subnet Broadcast Target calculated: {BROADCAST_IP}")

        # 2. Bind the RX Socket (Listen everywhere)
        _sock_rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _sock_rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _sock_rx.bind(('0.0.0.0', UDP_PORT))
        _sock_rx.setblocking(False)

        # 3. Bind the TX Socket (Explicitly tied to the Pico's IP)
        _sock_tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _sock_tx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        SO_BROADCAST = getattr(socket, "SO_BROADCAST", 32)
        try:
            _sock_tx.setsockopt(socket.SOL_SOCKET, SO_BROADCAST, 1)
        except Exception:
            pass
        
        # CRITICAL FIX: Bind explicitly to the wlan IP to prevent EINVAL Error 22
        # Port 0 lets the hardware pick a random ephemeral port for outbound traffic
        _sock_tx.bind((ip, 0)) 
        _sock_tx.setblocking(False)
        
        logger.info(TAG, f"UDP Direct sockets ready on port {UDP_PORT}")
        return True
        
    except Exception as e:
        logger.error(TAG, f"Failed to init WiFi-Direct: {e}")
        _sock_rx = None
        _sock_tx = None
        return False

def is_available():
    return _sock_rx is not None and _sock_tx is not None

async def tx_task(egress_queue):
    """Async task: drain WiFi-Direct egress queue and broadcast via UDP."""
    logger.info(TAG, "WiFi-Direct TX task started")
    
    while True:
        try:
            if is_available() and not egress_queue.is_empty():
                pkt = egress_queue.pop()
                if pkt:
                    payload_str = json.dumps(pkt)
                    
                    async with _udp_lock:
                        try:
                            _sock_tx.sendto(payload_str.encode('utf-8'), (BROADCAST_IP, UDP_PORT))
                            logger.debug(TAG, f"Broadcasted {pkt.get('type', 'data')} to UDP mesh")
                        except Exception as e:
                            logger.error(TAG, f"UDP sendto error: {e}")
                            
        except Exception as e:
            logger.error(TAG, f"TX task error: {e}")

        await asyncio.sleep_ms(100)

async def rx_task(ingress_queue, neighbour_table):
    """Async task: listen for UDP broadcasts and route them."""
    logger.info(TAG, "WiFi-Direct RX task started")
    
    while True:
        try:
            if is_available():
                data = None
                addr = None
                
                try:
                    # Notice no lock needed here anymore! 
                    # We are reading from _sock_rx while TX uses _sock_tx
                    data, addr = _sock_rx.recvfrom(2048)
                except OSError:
                    pass 

                if data:
                    msg_str = data.decode('utf-8')
                    try:
                        msg_obj = json.loads(msg_str)
                    except ValueError:
                        continue 

                    msg_type = str(msg_obj.get("type") or "")
                    msg_src = str(msg_obj.get("node_id") or msg_obj.get("src") or "unknown")

                    if msg_src == config.NODE_ID:
                        continue 

                    if msg_type == "hello":
                        neighbour_table.update(
                            msg_src,
                            protocols=["WiFi-Direct"],
                            capabilities=msg_obj.get("capabilities", ["WiFi-Direct"])
                        )
                        continue

                    ingress_queue.push(msg_obj.get("priority", packet.PRIORITY_NORMAL), msg_obj)

        except Exception as e:
            logger.error(TAG, f"RX task error: {e}")

        await asyncio.sleep_ms(200)

async def hello_task(neighbour_table):
    """Async task: periodically broadcast our presence to the local network."""
    import uasyncio as asyncio
    logger.info(TAG, "WiFi-Direct Hello task started")
    
    while True:
        try:
            if is_available():
                hello = create_hello_payload()
                
                if "WiFi-Direct" not in hello["capabilities"]:
                    hello["capabilities"].append("WiFi-Direct")

                payload_str = json.dumps(hello)
                
                async with _udp_lock:
                    try:
                        _sock_tx.sendto(payload_str.encode('utf-8'), (BROADCAST_IP, UDP_PORT))
                        logger.debug(TAG, "Broadcasted WiFi-Direct Hello") 
                    except Exception as e:
                        logger.error(TAG, f"Hello broadcast failed: {e}")
                        
        except Exception as e:
            logger.error(TAG, f"Hello task error: {e}")

        await asyncio.sleep(config.HELLO_INTERVAL)
