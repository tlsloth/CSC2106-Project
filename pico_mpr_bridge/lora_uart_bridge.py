from machine import UART, Pin
import utime
import network
import ujson

try:
    from umqtt.robust import MQTTClient
except ImportError:
    from umqtt.simple import MQTTClient

# ── UART Config (GP0=TX, GP1=RX) ─────────────────────────────────
uart = UART(0, baudrate=9600, tx=Pin(0), rx=Pin(1))

# ── WiFi Config ───────────────────────────────────────────────────
WIFI_SSID     = "SINGTEL-93KM"
WIFI_PASSWORD = "fddxftv82d"

# ── MQTT Config ───────────────────────────────────────────────────
MQTT_BROKER   = "192.168.1.9"
MQTT_PORT     = 1883
MQTT_USER     = ""
MQTT_PASSWORD = ""
MQTT_CLIENT_ID = "pico_uart_bridge"
MQTT_TOPIC_DATA = "mesh/data/{node}"
MQTT_TOPIC_LATEST = "mesh/latest/{node}"

mqtt_client = None

# ── WiFi Connect ──────────────────────────────────────────────────
def wifi_connect():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        print("WiFi already connected:", wlan.ifconfig()[0])
        return True
    print("Connecting to WiFi:", WIFI_SSID)
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)
    for i in range(20):
        if wlan.isconnected():
            print("WiFi connected:", wlan.ifconfig()[0])
            return True
        utime.sleep_ms(500)
    print("WiFi failed")
    return False


# ── MQTT Connect / Publish ────────────────────────────────────────
def mqtt_connect():
    global mqtt_client
    try:
        # Ensure WiFi is up before creating MQTT socket
        if not wifi_connect():
            print("MQTT connect blocked: WiFi unavailable")
            mqtt_client = None
            return False

        mqtt_client = MQTTClient(
            MQTT_CLIENT_ID,
            MQTT_BROKER,
            port=MQTT_PORT,
            user=MQTT_USER or None,
            password=MQTT_PASSWORD or None,
            keepalive=60,
        )
        mqtt_client.connect()
        print("MQTT connected:", MQTT_BROKER, MQTT_PORT)
        return True
    except Exception as e:
        mqtt_client = None
        print("MQTT connect failed:", e)
        return False


def mqtt_publish(node, payload_str):
    global mqtt_client
    if mqtt_client is None:
        print("MQTT client not ready, connecting...")
        if not mqtt_connect():
            return False

    topic_data = MQTT_TOPIC_DATA.format(node=node)
    topic_latest = MQTT_TOPIC_LATEST.format(node=node)
    try:
        payload_bytes = payload_str.encode("utf-8")

        # Live stream topic
        mqtt_client.publish(topic_data.encode("utf-8"), payload_bytes)
        # Retained latest value topic for fast dashboard restore
        mqtt_client.publish(topic_latest.encode("utf-8"), payload_bytes, True)

        print("MQTT TX:", topic_data, payload_str)
        print("MQTT TX(retained):", topic_latest, payload_str)
        return True
    except Exception as e:
        print("MQTT publish failed:", e)
        # Try one reconnect and one retry
        if not mqtt_connect():
            return False
        try:
            payload_bytes = payload_str.encode("utf-8")
            mqtt_client.publish(topic_data.encode("utf-8"), payload_bytes)
            mqtt_client.publish(topic_latest.encode("utf-8"), payload_bytes, True)
            print("MQTT TX(retry):", topic_data, payload_str)
            print("MQTT TX(retained,retry):", topic_latest, payload_str)
            return True
        except Exception as e2:
            print("MQTT retry failed:", e2)
            return False

# ── JSON Translation ──────────────────────────────────────────────
def translate_to_json(raw, node, rssi):
    parts = {}
    for item in raw.split(','):
        k, v = item.split(':')
        parts[k.strip()] = float(v.strip())
    parts['node'] = node
    parts['rssi'] = rssi
    return ujson.dumps(parts)

# ── Process one line from Arduino ────────────────────────────────
def process_line(line):
    try:
        data = ujson.loads(line)
        raw  = data.get('raw', '')
        node = data.get('node', '?')
        rssi = data.get('rssi', 0)

        json_str = translate_to_json(raw, node, rssi)
        print("JSON:", json_str)

        if not mqtt_publish(node, json_str):
            print("MQTT skipped or failed for node", node)

    except Exception as e:
        print("Parse error:", e, "| Line:", line)

# ── Main ──────────────────────────────────────────────────────────
def main():
    print("\n=== Pico W UART Bridge ===")

    wifi_ok = wifi_connect()
    if not wifi_ok:
        print("No WiFi — continuing without network")
    else:
        mqtt_connect()

    print("Listening on UART (GP0/GP1)...\n")
    buf = b''

    while True:
        if uart.any():
            chunk = uart.read(uart.any())
            if chunk:
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    line = line.strip()
                    if line:
                        decoded = line.decode('utf-8', 'ignore')
                        print("UART RX:", decoded)
                        process_line(decoded)
        utime.sleep_ms(10)

main()