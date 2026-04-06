# CSC2106 IoT Mesh Network — Multi-Protocol Bridge with MPR Routing

A hybrid IoT mesh network that bridges **BLE**, **LoRa**, and **WiFi** sensor nodes through Raspberry Pi Pico W bridges using OLSR-inspired Multi-Point Relay (MPR) routing. Sensor data is aggregated and forwarded to an MQTT broker, visualised on a real-time Flask dashboard.

## Architecture

```
  LoRa Sensor (DHT22)             BLE Sensor (HC-SR04)
  [Maker UNO + RFM95W]           [Pico W — Central only]
         |                                |
     [LoRa RF]                       [BLE GATT]
         |                                |
         v                                v
  ┌─────────────────────────────────────────────┐
  │       Pico W  —  MPR Bridge Node            │
  │                                             │
  │  UART/LoRa RX ──┐       ┌── BLE RX Server   │
  │                  v       v                  │
  │         [ Ingress Priority Queue ]          │
  │                    |                        │
  │        [ Translator + Router ]              │
  │        (Dijkstra + AODV cache)              │
  │                    |                        │
  │         [ Egress Priority Queue ]           │
  │              |           |                  │
  │        WiFi/MQTT TX LoRa/BLE TX             │
  └─────────────────────────────────────────────┘
                       |
                 [MQTT publish]
                       |
                       v
             ┌─────────────────┐
             │  Mosquitto MQTT │
             │     Broker      │
             └────────┬────────┘
                      |
                      v
             ┌─────────────────┐
             │ Flask Dashboard │
             │   (port 5050)   │
             └─────────────────┘
```

Multiple bridges can mesh together via WiFi-Direct (UDP broadcast), MQTT hello, and LoRa to form a self-healing multi-hop network.

## Directory Structure

```
CSC2106-Project/
├── README.md                          # This file
├── ble_sensor/                        # BLE ultrasonic sensor firmware (Pico W)
│   ├── BLE_MAIN.py                    #   HC-SR04 + BLE central mesh client
│   └── ble.py                         #   BLE edge helper (alternative mode)
├── lora_sensor_arduino/               # LoRa temperature sensor firmware (Arduino)
│   ├── README.md                      #   Arduino setup guide
│   ├── lora_sensor_edge/              #   DHT22 + LoRa sensor sketch
│   │   ├── lora_sensor_edge.ino
│   │   ├── BridgeMesh.cpp
│   │   └── BridgeMesh.h
│   └── maker_uno_lora_uart_bridge/    #   UART ↔ LoRa RF bridge sketch
│       └── maker_uno_lora_uart_bridge.ino
└── pico_mpr_bridge/                   # Bridge node firmware (Pico W)
    ├── main.py                        #   Entry point — hardware init + asyncio loop
    ├── config.py                      #   All tuneable parameters
    ├── lora_uart_bridge.py            #   UART↔LoRa helper
    ├── python_dashboard.py            #   Flask web dashboard + MQTT subscriber
    ├── requirements-dashboard.txt     #   Dashboard Python dependencies
    ├── upload_to_pico.bat             #   Windows upload script
    ├── upload_to_pico_macOS.sh        #   macOS upload script
    ├── core/
    │   ├── packet.py                  #   Packet format, header parsing
    │   ├── priority_queue.py          #   Heapq-based priority queue
    │   ├── neighbour.py               #   Neighbour table + Hello protocol
    │   ├── router.py                  #   Dijkstra routing + AODV cache
    │   ├── mpr.py                     #   OLSR MPR selection
    │   ├── translator.py              #   Cross-protocol packet translation
    │   └── security.py                #   Join authentication
    ├── interfaces/
    │   ├── ble_interface.py           #   BLE GATT server + bridge discovery
    │   ├── uart_lora_interface.py     #   LoRa over UART (Pico ↔ Maker UNO)
    │   ├── wifi_interface.py          #   WiFi + MQTT pub/sub
    │   └── wifi_direct_interface.py   #   WiFi-Direct UDP broadcast
    ├── utils/
    │   ├── logger.py                  #   Serial logger with severity levels
    │   └── watchdog.py                #   Software watchdog
    └── lib/
        ├── aioble/                    #   MicroPython async BLE library
        └── umqtt/                     #   MicroPython MQTT client
```

## Hardware Requirements

| Component | Quantity | Purpose |
|-----------|----------|---------|
| **Raspberry Pi Pico W** | 1+ per bridge, 1 per BLE sensor | Bridge node / BLE sensor |
| **Maker UNO** (or Arduino Uno) | 1 per LoRa sensor, 1 per LoRa bridge | LoRa sensor edge / UART↔LoRa bridge |
| **SX1276 RFM95W LoRa Shield** (915 MHz) | 1 per Maker UNO | LoRa radio |
| **DHT22 Temperature/Humidity Sensor** | 1 per LoRa sensor | Temperature + humidity data |
| **HC-SR04 Ultrasonic Sensor** | 1 per BLE sensor | Distance measurement |
| **USB cables** | As needed | Programming + power |
| **PC / Laptop** | 1 | MQTT broker + dashboard host |

## Software Requirements

| Software | Version | Purpose |
|----------|---------|---------|
| **MicroPython** | ≥ 1.22 (Pico W build) | Bridge + BLE sensor firmware |
| **mpremote** | Latest | Upload firmware to Pico W |
| **Arduino IDE** | ≥ 2.0 | LoRa sensor + UART bridge firmware |
| **Python** | ≥ 3.10 | Dashboard host |
| **Mosquitto** | ≥ 2.0 | MQTT broker |
| **Flask** | 3.0.3 | Dashboard web server |
| **paho-mqtt** | 2.1.0 | Dashboard MQTT client |

### Arduino Libraries (install via Library Manager)

- **LoRa** by Sandeep Mistry
- **RadioHead** (for BridgeMesh)
- **DHT sensor library** by Adafruit

## Configuration

All bridge parameters are in `pico_mpr_bridge/config.py`. Each bridge node needs its own copy with the values below adjusted.

### Node Identity

```python
NODE_ID         = "bridge_A"          # Unique name per bridge (bridge_A, bridge_B, ...)
NODE_ROLE       = "bridge"
CAPABILITIES    = ["BLE"]             # Protocols this bridge supports: "BLE", "LoRa", "WiFi-Direct"
```

### WiFi / MQTT

```python
WIFI_SSID       = "YourNetwork"       # WiFi network name
WIFI_PASSWORD   = "YourPassword"      # WiFi password
MQTT_BROKER     = "192.168.137.1"     # IP of the machine running Mosquitto
MQTT_PORT       = 1883
```

### LoRa (UART Bridge)

```python
LORA_TRANSPORT  = "UART"              # Use UART bridge to Maker UNO
UART_LORA_BAUD  = 115200
UART_LORA_TX_PIN = 0                  # GP0
UART_LORA_RX_PIN = 1                  # GP1
LORA_FREQ       = 915.0               # MHz — must match all nodes
LORA_SF         = 9                   # Spreading factor
```

### BLE Sensor

```python
BLE_DEVICE_NAME     = "PicoUltrasonic"       # BLE sensor advertised name
BLE_TRUSTED_SENSORS = ["PicoUltrasonic"]     # Sensors allowed to join
BLE_SERVICE_UUID    = 0xFFF0
BLE_CHAR_UUID       = 0xFFF1
```

### Mesh / Routing

```python
MESH_NETWORK_NAME = "CSC2106_MESH"    # Must match across all nodes
MESH_JOIN_KEY     = "mesh_key_v1"     # Shared join key
HELLO_INTERVAL    = 15                # Seconds between hello broadcasts
HELLO_TIMEOUT     = 90                # Seconds before declaring neighbour dead
PACKET_TTL        = 5                 # Max hops
```

### BLE Sensor Configuration

Edit the constants at the top of `ble_sensor/BLE_MAIN.py`:

```python
NODE_ID           = "ble_sensor_A"          # Unique per BLE sensor
MESH_JOIN_KEY     = "mesh_key_v1"           # Must match bridge config
MESH_NETWORK_NAME = "CSC2106_MESH"          # Must match bridge config
MESH_TARGET_DST   = "dashboard_main"        # Destination for sensor data
```

### LoRa Sensor Configuration

Edit the defines in `lora_sensor_arduino/lora_sensor_edge/lora_sensor_edge.ino`:

- `NODE_ID` — unique sensor name (e.g. `"lora_sensor_A"`)
- LoRa frequency, spreading factor, and sync word must match `config.py`

## How to Run

### 1. Set Up the MQTT Broker

Install and start Mosquitto on your PC/laptop:

```bash
# Windows (after installing Mosquitto)
mosquitto -v

# Or with a listener config allowing anonymous connections:
mosquitto -c mosquitto.conf -v
```

Ensure the broker is listening on port **1883** and is reachable from the Pico W over WiFi.

### 2. Flash MicroPython on Pico W

1. Download the latest MicroPython UF2 for **Pico W** from [micropython.org](https://micropython.org/download/RPI_PICO_W/)
2. Hold **BOOTSEL**, plug in the Pico W via USB, release BOOTSEL
3. Drag-and-drop the `.uf2` file onto the `RPI-RP2` drive

### 3. Upload Bridge Firmware

Edit `pico_mpr_bridge/config.py` with correct WiFi credentials, MQTT broker IP, and a unique `NODE_ID`.

**Windows:**
```cmd
cd pico_mpr_bridge
upload_to_pico.bat COM5
```

**macOS:**
```bash
cd pico_mpr_bridge
chmod +x upload_to_pico_macOS.sh
./upload_to_pico_macOS.sh /dev/cu.usbmodem*
```

The script uploads all Python files, `core/`, `interfaces/`, `utils/`, and `lib/` folders to the Pico W, then resets it.

> **Tip:** Install mpremote with `pip install mpremote` if not already installed.

### 4. Upload BLE Sensor Firmware

Flash MicroPython on a second Pico W, then copy the BLE sensor files:

```cmd
mpremote connect COM6 fs cp ble_sensor/BLE_MAIN.py :main.py
```

Also upload the `lib/aioble/` folder if not already on the device:

```cmd
mpremote connect COM6 fs cp -r pico_mpr_bridge/lib/aioble :lib/aioble
```

Wire the HC-SR04:
- **TRIG** → GP0
- **ECHO** → GP1
- **VCC** → 5V, **GND** → GND

### 5. Upload LoRa Sensor / UART Bridge Firmware

**LoRa Sensor Edge (Maker UNO #1):**
1. Open `lora_sensor_arduino/lora_sensor_edge/lora_sensor_edge.ino` in Arduino IDE
2. Install required libraries (LoRa, RadioHead, DHT)
3. Select **Board → Arduino Uno**, correct COM port
4. Upload

Wire the DHT22: data pin → defined pin in sketch (see `lora_sensor_edge.ino`).

**UART↔LoRa Bridge (Maker UNO #2):**
1. Open `lora_sensor_arduino/maker_uno_lora_uart_bridge/maker_uno_lora_uart_bridge.ino`
2. Upload to the Maker UNO connected to the Pico W bridge

Wire UART between Pico W and Maker UNO:
- Pico **GP0 (TX)** → Maker UNO **RX (pin 0)**
- Pico **GP1 (RX)** → Maker UNO **TX (pin 1)**
- Common **GND**

### 6. Start the Dashboard

On your PC (the same machine running the MQTT broker):

```bash
cd pico_mpr_bridge
pip install -r requirements-dashboard.txt
python python_dashboard.py
```

Open **http://localhost:5050** in a browser. The dashboard shows:
- Real-time sensor telemetry (temperature, humidity, distance)
- Force-directed network topology graph
- Node status and routing information

### 7. Verify Operation

Open a serial monitor to the bridge Pico W:

```cmd
mpremote connect COM5 repl
```

You should see logs like:

```
[INFO] WiFi connected: 192.168.137.42
[INFO] MQTT connected to 192.168.137.1:1883
[INFO] BLE advertising as bridge_A ...
[INFO] LoRa UART bridge ready on GP0/GP1
[INFO] HELLO broadcast sent (seq=1)
[INFO] JOIN_REQ from ble_sensor_A — accepted, token=ab3f...
[INFO] Sensor data from ble_sensor_A: distance=23cm
[INFO] Route: ble_sensor_A → bridge_A → MQTT (cost=60)
```

## Multi-Bridge Deployment

For a multi-bridge mesh, flash each Pico W with a unique `NODE_ID` and matching `CAPABILITIES`:

| Bridge | NODE_ID | CAPABILITIES | Links |
|--------|---------|--------------|-------|
| Bridge A | `bridge_A` | `["BLE", "LoRa"]` | BLE sensor + LoRa sensor |
| Bridge B | `bridge_B` | `["BLE"]` | BLE sensor only |
| Bridge C | `bridge_C` | `["LoRa", "WiFi-Direct"]` | LoRa sensor + peer mesh |

All bridges connect to the same WiFi/MQTT broker. The Dijkstra router automatically discovers neighbours via Hello messages and computes least-cost paths across protocols.

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `mpremote` not found | `pip install mpremote` |
| Cannot connect to Pico W COM port | Check Device Manager for correct port; ensure no other serial monitor is open |
| WiFi connection fails | Verify SSID/password in `config.py`; check `WIFI_CONNECT_ATTEMPTS` and `WIFI_CONNECT_TIMEOUT_S` |
| MQTT connection refused | Ensure Mosquitto is running and allows anonymous connections; verify `MQTT_BROKER` IP |
| BLE sensor not detected | Confirm `BLE_DEVICE_NAME` matches the sensor's advertised name; check `BLE_TRUSTED_SENSORS` |
| LoRa no communication | Verify frequency (915 MHz), spreading factor, and sync word match on all devices |
| UART bridge not responding | Check TX/RX wiring (cross-connect), baud rate = 115200, common GND |
| Dashboard shows no data | Confirm MQTT broker IP/port; check browser console for errors at `http://localhost:5050` |
| Neighbour disappears from routing table | Increase `HELLO_TIMEOUT` or decrease `HELLO_INTERVAL` in `config.py` |
