# config.py — All tuneable parameters for the MPR Bridge

# Node identity
NODE_ID         = "bridge_01"
NODE_ROLE       = "bridge"          # "bridge" | "sensor" | "dashboard"
CAPABILITIES    = ["LoRa", "BLE", "WiFi", "MQTT"]

# WiFi credentials
WIFI_SSID       = "Gigachad hotspot"
WIFI_PASSWORD   = "95610309"

# MQTT broker
MQTT_BROKER     = "192.168.1.100"
MQTT_PORT       = 1883
MQTT_USER       = ""
MQTT_PASSWORD   = ""
MQTT_KEEPALIVE  = 60

# Discovery
HELLO_INTERVAL  = 15                # seconds between Hello broadcasts
HELLO_TIMEOUT   = 45                # 3x interval -> declare neighbour dead

# Cost model (cross-protocol translation costs)
COST_NATIVE     = 1                 # LoRa->LoRa, WiFi->WiFi, BLE->BLE
COST_LORA_WIFI  = 5                 # LoRa -> Bridge -> WiFi
COST_BLE_WIFI   = 4                 # BLE -> Bridge -> WiFi
COST_LORA_BLE   = 6                 # LoRa -> Bridge -> BLE

# Priority thresholds
TEMP_ALERT_THRESHOLD    = 40.0      # Celsius — above this = HIGH priority
DISTANCE_ALERT_MIN      = 10        # cm — below this = intrusion alert

# MQTT topics (templated with node_id)
MQTT_DATA_TOPIC     = "mesh/data/{node_id}"
MQTT_ALERT_TOPIC    = "mesh/alert/{node_id}"
MQTT_TOPO_TOPIC     = "mesh/topology/{node_id}"
MQTT_CMD_TOPIC      = "mesh/cmd/{node_id}"
MQTT_HELLO_TOPIC    = "mesh/hello"

# LoRa parameters (SX1276 / RFM95W)
LORA_FREQ       = 915.0            # MHz (915 MHz ISM band)
LORA_BW         = 125.0            # kHz bandwidth
LORA_SF         = 9                # Spreading factor
LORA_CR         = 5                # Coding rate (4/5)
LORA_SYNC_WORD  = 0x12             # Project-wide sync word
LORA_TX_POWER   = 14               # dBm

# LoRa SPI pin mapping (SX1276 RFM95W shield)
LORA_SPI_ID     = 0
LORA_PIN_SCK    = 18
LORA_PIN_MOSI   = 19
LORA_PIN_MISO   = 16
LORA_PIN_CS     = 17
LORA_PIN_RESET  = 20
LORA_PIN_DIO0   = 21               # SX127x uses DIO0 (not DIO1)

# BLE parameters (use 16-bit UUIDs for Pico W compatibility)
BLE_SERVICE_UUID    = 0xFFF0       # BLE sensor service UUID
BLE_CHAR_UUID       = 0xFFF1       # Distance characteristic UUID
BLE_DEVICE_NAME     = "PicoUltrasonic"                        # Name of BLE sensor to connect to
BLE_SCAN_DURATION   = 5000          # ms per scan cycle
BLE_SCAN_INTERVAL   = 10000         # ms between scan cycles
BLE_CONN_TIMEOUT    = 10000         # ms connection timeout
BLE_DISCOVERY_DELAY_MS = 250        # wait after connect before service discovery
BLE_DISCOVERY_TIMEOUT_MS = 4000     # service/characteristic discovery timeout
BLE_DISCOVERY_RETRIES = 3           # retries for flaky BLE discovery on Pico W

# Packet settings
PACKET_TTL          = 5             # Max hops before packet is dropped
MAX_PAYLOAD_SIZE    = 200           # bytes — fragments if larger

# Logging
LOG_LEVEL       = "DEBUG"           # "DEBUG" | "INFO" | "WARN" | "ERROR"
