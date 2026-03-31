# interfaces/wifi_interface.py — WiFi connection + MQTT pub/sub tasks

import json
import time
import config
from utils import logger
from core import packet
from core.neighbour import create_hello_payload, parse_hello
# Removed security imports to simplify data routing first; can add back later if needed

TAG = "WiFi-MQTT"

_wlan = None
_mqtt = None
_pending_msgs = [] # Global queue for incoming MQTT messages

def _wifi_status_text(status):
    mapping = {
        0: "STAT_IDLE", 1: "STAT_CONNECTING", 2: "STAT_WRONG_PASSWORD",
        3: "STAT_NO_AP_FOUND", 4: "STAT_CONNECT_FAIL", 5: "STAT_GOT_IP",
        -1: "STAT_CONNECT_FAIL", -2: "STAT_NO_AP_FOUND", -3: "STAT_WRONG_PASSWORD",
    }
    return mapping.get(status, str(status))

def _global_mqtt_callback(topic, msg):
    """Unified global callback for all MQTT subscriptions."""
    _pending_msgs.append((topic, msg))

def init():
    """Connect to WiFi and initialise MQTT client."""
    global _wlan, _mqtt

    try:
        import network
        _wlan = network.WLAN(network.STA_IF)
        _wlan.active(True)
        
        _wlan.config(pm=0)

        if not _wlan.isconnected():
            attempts = int(getattr(config, "WIFI_CONNECT_ATTEMPTS", 3) or 3)
            for attempt in range(1, attempts + 1):
                logger.info(TAG, f"Connecting to WiFi '{config.WIFI_SSID}' (attempt {attempt}/{attempts})...")
                
                try: _wlan.disconnect()
                except: pass

                _wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)

                timeout = 15
                while not _wlan.isconnected() and timeout > 0:
                    time.sleep(1)
                    timeout -= 1

                if _wlan.isconnected(): break
                time.sleep(1)

        if _wlan.isconnected():
            ip = _wlan.ifconfig()[0]
            logger.info(TAG, f"WiFi connected, IP: {ip}")
        else:
            logger.error(TAG, f"WiFi connection failed (status={_wifi_status_text(_wlan.status())})")
            return False

    except Exception as e:
        logger.error(TAG, f"WiFi init failed: {e}")
        return False

    # Initialise MQTT
    try:
        from umqtt.robust import MQTTClient

        client_id = getattr(config, "NODE_ID", "pico_bridge")
        _mqtt = MQTTClient(
            client_id,
            config.MQTT_BROKER,
            port=getattr(config, "MQTT_PORT", 1883),
            keepalive=getattr(config, "MQTT_KEEPALIVE", 60),
        )

        if getattr(config, "MQTT_USER", ""):
            _mqtt.user = config.MQTT_USER
            _mqtt.pswd = config.MQTT_PASSWORD

        _mqtt.set_callback(_global_mqtt_callback)
        _mqtt.connect()
        
        _mqtt.sock.setblocking(False)
        
        logger.info(TAG, f"MQTT connected to {config.MQTT_BROKER}:{getattr(config, 'MQTT_PORT', 1883)}")
        return True
    except Exception as e:
        logger.error(TAG, f"MQTT init failed: {e}")
        _mqtt = None
        return False

def is_available():
    return _wlan is not None and _wlan.isconnected() and _mqtt is not None

def mqtt_publish(topic, message, retain=False):
    """Publish a message to an MQTT topic."""
    if _mqtt is None: return False
    try:
        if isinstance(message, str):
            message = message.encode("utf-8")
        _mqtt.publish(topic.encode("utf-8"), message, retain=retain)
        logger.debug(TAG, f"Published to {topic}")
        return True
    except Exception as e:
        logger.error(TAG, f"MQTT publish error: {e}")
        _try_reconnect()
        return False

def mqtt_subscribe(topic):
    """Subscribe to an MQTT topic (callback is handled globally)."""
    if _mqtt is None: return False
    try:
        _mqtt.subscribe(topic.encode("utf-8"))
        logger.info(TAG, f"Subscribed to {topic}")
        return True
    except Exception as e:
        logger.error(TAG, f"MQTT subscribe error: {e}")
        return False

def _try_reconnect():
    global _mqtt
    try:
        if _mqtt:
            _mqtt.connect()
            _mqtt.sock.setblocking(False) # Re-apply non-blocking on reconnect
            logger.info(TAG, "MQTT reconnected")
    except Exception as e:
        logger.error(TAG, f"MQTT reconnect failed: {e}")
        _mqtt = None

async def tx_task(egress_queue):
    """Async task: drain egress queue and publish to Dashboard."""
    import uasyncio as asyncio
    logger.info(TAG, "WiFi/MQTT TX task started")
    
    while True:
        try:
            if is_available() and not egress_queue.is_empty():
                pkt = egress_queue.pop()
                if pkt:
                    msg_type = pkt.get("type", "")
                    
                    if msg_type in ["data", "sensor"]:
                        node_id = pkt.get("src", "unknown")
                        topic = f"mesh/data/{node_id}" # Matches your Flask app subscription
                        
                        # Inject this bridge's ID so the dashboard knows who routed it!
                        pkt["hop_dst"] = getattr(config, "NODE_ID", "bridge")
                        
                        payload_str = json.dumps(pkt)
                        mqtt_publish(topic, payload_str)
                    
                    # You can still use translator for other packet types if needed here
                    # result = translate_to_mqtt(pkt) ...
                    
        except Exception as e:
            logger.error(TAG, f"TX error: {e}")

        await asyncio.sleep_ms(100)

async def rx_task(ingress_queue, neighbour_table):
    """Async task: check for incoming MQTT command messages."""
    import uasyncio as asyncio
    logger.info(TAG, "WiFi/MQTT RX task started")

    if is_available():
        # Subscribe to topics
        cmd_topic = getattr(config, "MQTT_CMD_TOPIC", "mesh/cmd/{node_id}").format(node_id=config.NODE_ID)
        mqtt_subscribe(cmd_topic)
        mqtt_subscribe(getattr(config, "MQTT_HELLO_TOPIC", "mesh/hello"))

    while True:
        try:
            if _mqtt is not None:
                try:
                    _mqtt.check_msg() # Now completely non-blocking!
                except OSError:
                    pass # Ignore standard non-blocking 'no data' errors

                while _pending_msgs:
                    topic, msg = _pending_msgs.pop(0)
                    if isinstance(topic, bytes): topic = topic.decode("utf-8")
                    if isinstance(msg, bytes): msg = msg.decode("utf-8")

                    # Handle hello messages
                    if "hello" in topic:
                        hello = parse_hello(msg)
                        if hello:
                            neighbour_table.update(
                                hello["node_id"], protocols=["WiFi", "MQTT"],
                                capabilities=hello.get("capabilities", ["WiFi", "MQTT"])
                            )
                        continue

                    # Put incoming commands into the ingress queue
                    try:
                        msg_obj = json.loads(msg)
                        ingress_queue.push(packet.PRIORITY_NORMAL, msg_obj)
                    except Exception:
                        pass # Ignore malformed json

        except Exception as e:
            logger.error(TAG, f"RX error: {e}")

        await asyncio.sleep_ms(200)

async def hello_task(neighbour_table):
    import uasyncio as asyncio
    logger.info(TAG, "WiFi/MQTT Hello task started")
    
    while True:
        try:
            if is_available():
                # 1. Publish the standard Hello
                hello = create_hello_payload()
                hello_topic = getattr(config, "MQTT_HELLO_TOPIC", "mesh/hello")
                mqtt_publish(hello_topic, json.dumps(hello))
                
                # 2. Publish the Topology
                topo_topic = getattr(config, "MQTT_TOPO_TOPIC", "mesh/topology/{node_id}").format(node_id=config.NODE_ID)
                topo_data = json.dumps({
                    "node_id": config.NODE_ID,
                    "neighbours": neighbour_table.to_dict(),
                })
                mqtt_publish(topo_topic, topo_data)
                
        except Exception as e:
            logger.error(TAG, f"Hello/Topo publish error: {e}")
            
        await asyncio.sleep(getattr(config, "HELLO_INTERVAL", 15))