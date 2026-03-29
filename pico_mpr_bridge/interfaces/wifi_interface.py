# interfaces/wifi_interface.py — WiFi connection + MQTT pub/sub tasks

import json
import time
import config
from utils import logger
from core import packet
from core.neighbour import create_hello_payload, parse_hello
from core.security import check_join_auth, check_node_token, generate_join_token

_node_tokens = {}

TAG = "WiFi"

_wlan = None
_mqtt = None


def _wifi_status_text(status):
    # MicroPython status values vary slightly by port; keep a safe fallback.
    mapping = {
        0: "STAT_IDLE",
        1: "STAT_CONNECTING",
        2: "STAT_WRONG_PASSWORD",
        3: "STAT_NO_AP_FOUND",
        4: "STAT_CONNECT_FAIL",
        5: "STAT_GOT_IP",
        -1: "STAT_CONNECT_FAIL",
        -2: "STAT_NO_AP_FOUND",
        -3: "STAT_WRONG_PASSWORD",
    }
    return mapping.get(status, str(status))


def init():
    """Connect to WiFi and initialise MQTT client."""
    global _wlan, _mqtt

    try:
        import network

        _wlan = network.WLAN(network.STA_IF)
        _wlan.active(True)

        if not _wlan.isconnected():
            attempts = int(getattr(config, "WIFI_CONNECT_ATTEMPTS", 3) or 3)
            timeout_s = int(getattr(config, "WIFI_CONNECT_TIMEOUT_S", 20) or 20)

            for attempt in range(1, attempts + 1):
                logger.info(
                    TAG,
                    "Connecting to WiFi '{}' (attempt {}/{})...".format(
                        config.WIFI_SSID,
                        attempt,
                        attempts,
                    ),
                )

                try:
                    _wlan.disconnect()
                except Exception:
                    pass

                _wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)

                timeout = timeout_s
                while not _wlan.isconnected() and timeout > 0:
                    time.sleep(1)
                    timeout -= 1

                if _wlan.isconnected():
                    break

                status = _wifi_status_text(_wlan.status())
                logger.warn(TAG, "WiFi attempt {} failed (status={})".format(attempt, status))
                time.sleep(1)

        if _wlan.isconnected():
            ip = _wlan.ifconfig()[0]
            logger.info(TAG, "WiFi connected, IP: {}".format(ip))
        else:
            status = _wifi_status_text(_wlan.status())
            logger.error(TAG, "WiFi connection failed (status={})".format(status))
            return False

    except Exception as e:
        logger.error(TAG, "WiFi init failed: {}".format(e))
        return False

    # Initialise MQTT
    try:
        from umqtt.robust import MQTTClient

        client_id = config.NODE_ID
        _mqtt = MQTTClient(
            client_id,
            config.MQTT_BROKER,
            port=config.MQTT_PORT,
            keepalive=config.MQTT_KEEPALIVE,
        )

        if config.MQTT_USER:
            _mqtt.user = config.MQTT_USER
            _mqtt.pswd = config.MQTT_PASSWORD

        _mqtt.connect()
        logger.info(TAG, "MQTT connected to {}:{}".format(config.MQTT_BROKER, config.MQTT_PORT))
        return True
    except Exception as e:
        logger.error(TAG, "MQTT init failed: {}".format(e))
        _mqtt = None
        return False


def is_available():
    return _wlan is not None and _wlan.isconnected() and _mqtt is not None


def mqtt_publish(topic, message, retain=False):
    """Publish a message to an MQTT topic."""
    if _mqtt is None:
        logger.warn(TAG, "MQTT not available, cannot publish")
        return False
    try:
        if isinstance(message, str):
            message = message.encode("utf-8")
        _mqtt.publish(topic.encode("utf-8"), message, retain=retain)
        logger.debug(
            TAG,
            "Published to {}: {} bytes (retain={})".format(topic, len(message), retain),
        )
        return True
    except Exception as e:
        logger.error(TAG, "MQTT publish error: {}".format(e))
        _try_reconnect()
        return False


def mqtt_subscribe(topic, callback):
    """Subscribe to an MQTT topic with a callback."""
    if _mqtt is None:
        return False
    try:
        _mqtt.set_callback(callback)
        _mqtt.subscribe(topic.encode("utf-8"))
        logger.info(TAG, "Subscribed to {}".format(topic))
        return True
    except Exception as e:
        logger.error(TAG, "MQTT subscribe error: {}".format(e))
        return False


def _try_reconnect():
    """Attempt to reconnect MQTT."""
    global _mqtt
    try:
        if _mqtt:
            _mqtt.connect()
            logger.info(TAG, "MQTT reconnected")
    except Exception as e:
        logger.error(TAG, "MQTT reconnect failed: {}".format(e))
        _mqtt = None


async def tx_task(egress_queue):
    """Async task: drain WiFi/MQTT egress queue and publish."""
    import uasyncio as asyncio
    from core.translator import translate_to_mqtt

    logger.info(TAG, "WiFi TX task started")
    while True:
        try:
            if is_available() and not egress_queue.is_empty():
                pkt = egress_queue.pop()
                if pkt:
                    result = translate_to_mqtt(pkt)
                    if result:
                        # Support single publish tuple and multi-publish list.
                        if isinstance(result, list):
                            for item in result:
                                if isinstance(item, tuple):
                                    topic = item[0]
                                    payload_str = item[1]
                                    retain = item[2] if len(item) > 2 else False
                                    mqtt_publish(topic, payload_str, retain=retain)
                        elif isinstance(result, tuple):
                            topic = result[0]
                            payload_str = result[1]
                            retain = result[2] if len(result) > 2 else False
                            mqtt_publish(topic, payload_str, retain=retain)
        except Exception as e:
            logger.error(TAG, "TX error: {}".format(e))

        await asyncio.sleep_ms(100)


async def rx_task(ingress_queue, neighbour_table):
    """Async task: check for incoming MQTT command messages."""
    import uasyncio as asyncio
    from core.translator import translate_from_mqtt

    logger.info(TAG, "WiFi RX task started")

    # Subscribe to command topic for this node
    cmd_topic = config.MQTT_CMD_TOPIC.format(node_id=config.NODE_ID)

    _pending_msgs = []

    def _on_message(topic, msg):
        _pending_msgs.append((topic, msg))

    if is_available():
        mqtt_subscribe(cmd_topic, _on_message)
        # Also subscribe to hello topic
        mqtt_subscribe(config.MQTT_HELLO_TOPIC, _on_message)
        # Subscribe to topology broadcasts from other bridges
        mqtt_subscribe("mesh/topology/#", _on_message)

    while True:
        try:
            if _mqtt is not None:
                _mqtt.check_msg()  # Non-blocking check

                while _pending_msgs:
                    topic, msg = _pending_msgs.pop(0)
                    if isinstance(topic, bytes):
                        topic = topic.decode("utf-8")

                    # Handle hello messages
                    if "hello" in topic:
                        hello = parse_hello(msg)
                        if hello:
                            neighbour_table.update(
                                hello["node_id"],
                                protocols=["WiFi", "MQTT"],
                                capabilities=hello.get("capabilities", ["WiFi", "MQTT"]),
                            )
                        continue

                    # Handle topology broadcasts from other bridges
                    if "topology" in topic:
                        try:
                            if isinstance(msg, (bytes, bytearray)):
                                msg = msg.decode("utf-8")
                            topo = json.loads(msg)
                            remote_id = topo.get("node_id")
                            remote_neighbours = topo.get("neighbours", {})
                            if remote_id and remote_id != config.NODE_ID:
                                neighbour_table.merge_remote(remote_id, remote_neighbours)
                                logger.debug(TAG, "Merged topology from {}".format(remote_id))
                        except Exception as e:
                            logger.warn(TAG, "Failed to parse topology: {}".format(e))
                        continue

                    # Handle command/sensor messages from mesh nodes — validate before enqueuing
                    try:
                        if isinstance(msg, (bytes, bytearray)):
                            msg_str = msg.decode("utf-8")
                        else:
                            msg_str = str(msg)
                        msg_obj = json.loads(msg_str)
                    except Exception:
                        logger.warn(TAG, "Failed to parse MQTT message as JSON: {}".format(topic))
                        continue
                    
                    msg_type = str(msg_obj.get("type") or "")
                    msg_node_id = str(msg_obj.get("node_id") or msg_obj.get("src") or "unknown")
                    
                    # Validate: join_req with auth, or other packets with token
                    if msg_type == "join_req":
                        ok, reason = check_join_auth(
                            msg_obj,
                            getattr(config, "MESH_NETWORK_NAME", ""),
                            getattr(config, "MESH_JOIN_KEY", ""),
                        )
                        if ok:
                            token = generate_join_token(
                                token_bytes=int(getattr(config, "MESH_JOIN_TOKEN_BYTES", 8) or 8),
                                entropy_hint=len(_node_tokens),
                            )
                            _node_tokens[msg_node_id] = token
                            logger.info(TAG, "Join accepted for {} via MQTT".format(msg_node_id))
                        else:
                            _node_tokens.pop(msg_node_id, None)
                            logger.warn(TAG, "Join rejected for {} ({})".format(msg_node_id, reason))
                        continue
                    
                    # For all other message types: token is mandatory
                    if not msg_type:
                        logger.warn(TAG, "MQTT message from {} has no type".format(msg_node_id))
                        continue
                    
                    token_ok, token_reason = check_node_token(
                        msg_obj,
                        _node_tokens.get(msg_node_id),
                        getattr(config, "MESH_JOIN_KEY", ""),
                    )
                    if not token_ok:
                        logger.warn(TAG, "Dropped MQTT {} from {} ({})".format(
                            msg_type, msg_node_id, token_reason))
                        continue
                    
                    # Token validated; translate and enqueue
                    pkt = translate_from_mqtt(topic, msg)
                    if pkt:
                        # Ensure src is from validated origin
                        pkt["src"] = msg_node_id
                        ingress_queue.push(pkt.get("priority", packet.PRIORITY_NORMAL), pkt)

        except Exception as e:
            logger.error(TAG, "RX error: {}".format(e))

        await asyncio.sleep_ms(500)


async def hello_task(neighbour_table):
    """Async task: periodically publish Hello on MQTT."""
    import uasyncio as asyncio

    logger.info(TAG, "WiFi Hello task started")
    while True:
        try:
            if is_available():
                hello = create_hello_payload()
                mqtt_publish(config.MQTT_HELLO_TOPIC, json.dumps(hello))
                logger.debug(TAG, "Sent MQTT Hello")

                # Also publish topology
                topo_topic = config.MQTT_TOPO_TOPIC.format(node_id=config.NODE_ID)
                topo_data = json.dumps({
                    "node_id": config.NODE_ID,
                    "neighbours": neighbour_table.to_dict(),
                })
                mqtt_publish(topo_topic, topo_data)
        except Exception as e:
            logger.error(TAG, "Hello publish error: {}".format(e))

        await asyncio.sleep(config.HELLO_INTERVAL)
