import network
import utime
import ujson

try:
    from umqtt.robust import MQTTClient
except ImportError:
    from umqtt.simple import MQTTClient

# WiFi settings for this machine's network
WIFI_SSID = "WJ"
WIFI_PASSWORD = "Weejer18"

# Must match your broker reachable by BOTH Pico W devices
MQTT_BROKER = "192.168.1.100"
MQTT_PORT = 1883
MQTT_USER = ""
MQTT_PASSWORD = ""
MQTT_CLIENT_ID = "pico_dashboard_endpoint"
MQTT_TOPIC_SUB = "mesh/data/+"


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

        node = data.get("node", "?")
        temp = data.get("T", None)
        hum = data.get("H", None)
        rssi = data.get("rssi", None)

        print("MQTT RX:", topic_str)
        print("DATA   :", data)
        print("Parsed : node={}, T={}, H={}, rssi={}".format(node, temp, hum, rssi))
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
    client.subscribe(MQTT_TOPIC_SUB.encode("utf-8"))
    print("MQTT connected:", MQTT_BROKER, MQTT_PORT)
    print("Subscribed to:", MQTT_TOPIC_SUB)
    return client


def main():
    print("\n=== Pico W MQTT Endpoint ===")
    wifi_connect()

    client = mqtt_connect_and_subscribe()

    while True:
        try:
            client.check_msg()
        except Exception as e:
            print("MQTT check failed, reconnecting:", e)
            utime.sleep(2)
            client = mqtt_connect_and_subscribe()
        utime.sleep_ms(100)


main()
