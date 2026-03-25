from machine import UART, Pin
import utime
import network
import ujson

# ── UART Config (GP0=TX, GP1=RX) ─────────────────────────────────
uart = UART(0, baudrate=9600, tx=Pin(0), rx=Pin(1))

# ── WiFi Config ───────────────────────────────────────────────────
WIFI_SSID     = "WJ"
WIFI_PASSWORD = "Weejer18"

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

        # ── Step 4: MQTT publish here ─────────────────────────────
        # mqtt_publish(json_str)
        # ── Step 5: CoAP publish here ─────────────────────────────
        # coap_publish(json_str)

    except Exception as e:
        print("Parse error:", e, "| Line:", line)

# ── Main ──────────────────────────────────────────────────────────
def main():
    print("\n=== Pico W UART Bridge ===")

    if not wifi_connect():
        print("No WiFi — continuing without network")

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