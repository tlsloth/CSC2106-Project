import network
import utime
import ujson

try:
    import usocket as socket
except ImportError:
    import socket

try:
    from umqtt.robust import MQTTClient
except ImportError:
    from umqtt.simple import MQTTClient

# WiFi settings for this machine's network
WIFI_SSID = "SINGTEL-93KM"
WIFI_PASSWORD = "fddxftv82d"

# Must match your broker reachable by BOTH Pico W devices
MQTT_BROKER = "192.168.1.9"
MQTT_PORT = 1883
MQTT_USER = ""
MQTT_PASSWORD = ""
MQTT_CLIENT_ID = "pico_dashboard_endpoint"
MQTT_TOPIC_SUB_DATA = "mesh/data/+"
MQTT_TOPIC_SUB_LATEST = "mesh/latest/+"
MQTT_RETRY_SECONDS = 3

latest_by_node = {}


def _node_from_topic(topic_str):
    parts = topic_str.split("/")
    if len(parts) >= 3 and parts[2]:
        return parts[2]
    return "?"


def _extract_fields(data, topic_str):
    """Support both legacy and standard bridge payload shapes."""
    # Legacy payload from lora_uart_bridge compatibility:
    # {"node":"A","T":31.2,"H":65.0,"rssi":-40}
    node = data.get("node")
    temp = data.get("T")
    hum = data.get("H")
    rssi = data.get("rssi")

    # Standard MPR payload:
    # {"src":"A","dst":"dashboard","data":{"temp":31.2,"humidity":65.0}, ...}
    if temp is None and hum is None and isinstance(data.get("data"), dict):
        payload = data.get("data", {})
        temp = payload.get("T", payload.get("temp", payload.get("temperature")))
        hum = payload.get("H", payload.get("humidity", payload.get("hum")))
        if rssi is None:
            rssi = payload.get("rssi")

    if not node:
        node = data.get("src") or _node_from_topic(topic_str)

    return str(node), temp, hum, rssi


def wifi_connect():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        print("WiFi already connected:", wlan.ifconfig()[0])
        return wlan

    print("Connecting WiFi:", WIFI_SSID)
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)
    for _ in range(30):
        if wlan.isconnected():
            print("WiFi connected:", wlan.ifconfig()[0])
            return wlan
        utime.sleep_ms(500)

    raise RuntimeError("WiFi connection failed")


def on_message(topic, msg):
    try:
        topic_str = topic.decode("utf-8") if isinstance(topic, (bytes, bytearray)) else str(topic)
        msg_str = msg.decode("utf-8") if isinstance(msg, (bytes, bytearray)) else str(msg)
        data = ujson.loads(msg_str)

        node, temp, hum, rssi = _extract_fields(data, topic_str)

        latest_by_node[node] = {
            "T": temp,
            "H": hum,
            "rssi": rssi,
            "topic": topic_str,
            "ts_ms": utime.ticks_ms(),
        }

        print("MQTT RX:", topic_str)
        print("DATA   :", data)
        print("Parsed : node={}, T={}, H={}, rssi={}".format(node, temp, hum, rssi))

        print("-- Dashboard Snapshot --")
        for n in sorted(latest_by_node):
            item = latest_by_node[n]
            print("{} | T={}C H={}% RSSI={} from {}".format(
                n,
                item.get("T"),
                item.get("H"),
                item.get("rssi"),
                item.get("topic"),
            ))
    except Exception as e:
        print("Message parse error:", e)


def mqtt_connect_and_subscribe():
    client = MQTTClient(
        MQTT_CLIENT_ID,
        MQTT_BROKER,
        port=MQTT_PORT,
        user=MQTT_USER or None,
        password=MQTT_PASSWORD or None,
        keepalive=60,
    )
    client.set_callback(on_message)
    client.connect()
    client.subscribe(MQTT_TOPIC_SUB_DATA.encode("utf-8"))
    client.subscribe(MQTT_TOPIC_SUB_LATEST.encode("utf-8"))
    print("MQTT connected:", MQTT_BROKER, MQTT_PORT)
    print("Subscribed to:", MQTT_TOPIC_SUB_DATA)
    print("Subscribed to:", MQTT_TOPIC_SUB_LATEST)
    return client


def broker_tcp_reachable():
    s = None
    try:
        addr = socket.getaddrinfo(MQTT_BROKER, MQTT_PORT)[0][-1]
        s = socket.socket()
        s.settimeout(3)
        s.connect(addr)
        return True
    except Exception as e:
        print("Broker TCP check failed:", e)
        return False
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass


def mqtt_connect_loop():
    while True:
        try:
            if not broker_tcp_reachable():
                print("MQTT broker not reachable at {}:{}".format(MQTT_BROKER, MQTT_PORT))
                utime.sleep(MQTT_RETRY_SECONDS)
                continue

            return mqtt_connect_and_subscribe()
        except Exception as e:
            print("MQTT connect failed, retrying:", e)
            utime.sleep(MQTT_RETRY_SECONDS)


def main():
    print("\n=== Pico W MQTT Endpoint ===")
    wifi_connect()

    client = mqtt_connect_loop()

    while True:
        try:
            client.check_msg()
        except Exception as e:
            print("MQTT check failed, reconnecting:", e)
            utime.sleep(2)
            client = mqtt_connect_loop()
        utime.sleep_ms(100)


main()
