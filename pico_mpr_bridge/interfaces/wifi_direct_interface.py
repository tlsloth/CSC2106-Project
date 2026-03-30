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

_sock = None
_udp_lock = asyncio.Lock()

def init():
    """Bind a non-blocking UDP socket to listen and broadcast."""
    global _sock
    try:
        _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Allow multiple bridges on the same network to use the port
        _sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _sock.bind(('', UDP_PORT))
        _sock.setblocking(False)
        
        logger.info(TAG, f"UDP Direct socket bound to port {UDP_PORT}")
        return True
    except Exception as e:
        logger.error(TAG, f"Failed to bind UDP socket: {e}")
        _sock = None
        return False

def is_available():
    return _sock is not None

async def tx_task(egress_queue):
    """Async task: drain WiFi-Direct egress queue and broadcast via UDP."""
    logger.info(TAG, "WiFi-Direct TX task started")
    
    while True:
        try:
            if is_available() and not egress_queue.is_empty():
                pkt = egress_queue.pop()
                if pkt:
                    # Native protocol! No MQTT translation needed.
                    # We just dump the raw routing dictionary into a string.
                    payload_str = json.dumps(pkt)
                    
                    # Request the hardware lock before transmitting
                    async with _udp_lock:
                        try:
                            _sock.sendto(payload_str.encode('utf-8'), (BROADCAST_IP, UDP_PORT))
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
                
                # Request the lock before checking the socket
                async with _udp_lock:
                    try:
                        # 2048 buffer is plenty for our mesh payloads
                        data, addr = _sock.recvfrom(2048)
                    except OSError:
                        # Expected behavior for non-blocking sockets when empty
                        pass 

                if data:
                    msg_str = data.decode('utf-8')
                    try:
                        msg_obj = json.loads(msg_str)
                    except ValueError:
                        continue # Ignore random internet garbage

                    msg_type = str(msg_obj.get("type") or "")
                    msg_src = str(msg_obj.get("node_id") or msg_obj.get("src") or "unknown")

                    # Ignore our own broadcasts echoing back
                    if msg_src == config.NODE_ID:
                        continue 

                    # 1. Handle Hello Packets (Topology Discovery)
                    if msg_type == "hello":
                        neighbour_table.update(
                            msg_src,
                            protocols=["WiFi-Direct"],
                            capabilities=msg_obj.get("capabilities", ["WiFi-Direct"])
                        )
                        continue

                    # 2. Handle Mesh Data Packets
                    # If it's not a hello, push it to the main router to handle
                    ingress_queue.push(msg_obj.get("priority", packet.PRIORITY_NORMAL), msg_obj)

        except Exception as e:
            logger.error(TAG, f"RX task error: {e}")

        await asyncio.sleep_ms(200)

async def hello_task(neighbour_table):
    """Async task: periodically broadcast our presence to the local network."""
    logger.info(TAG, "WiFi-Direct Hello task started")
    
    while True:
        try:
            if is_available():
                hello = create_hello_payload()
                
                # Inject WiFi-Direct specifically so neighbours know how we communicate
                if "WiFi-Direct" not in hello["capabilities"]:
                    hello["capabilities"].append("WiFi-Direct")

                payload_str = json.dumps(hello)
                
                async with _udp_lock:
                    try:
                        _sock.sendto(payload_str.encode('utf-8'), (BROADCAST_IP, UDP_PORT))
                    except Exception as e:
                        pass
        except Exception as e:
            logger.error(TAG, f"Hello task error: {e}")

        await asyncio.sleep(config.HELLO_INTERVAL)