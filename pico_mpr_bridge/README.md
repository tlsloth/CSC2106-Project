# MPR Bridge — Protocol-Aware Multi-Point Relay for Pico W

A MicroPython-based protocol bridge that receives data from BLE ultrasonic and LoRa temperature sensor nodes, translates payloads, and forwards everything to an MQTT dashboard over WiFi. Runs on a Raspberry Pi Pico W with a Waveshare LoRa shield (SX1262/LR1121).

## Architecture

```
LoRa Temp Node                    BLE Ultrasonic Node
     |                                   |
  [LoRa frame]                    [BLE notification]
     |                                   |
     v                                   v
 ┌──────────────────────────────────────────┐
 │          Pico W + LoRa (MPR Bridge)      │
 │                                          │
 │  LoRa RX ──┐          ┌── BLE RX        │
 │             v          v                 │
 │        [ Ingress Priority Queue ]        │
 │                  |                       │
 │           [ Translator ]                 │
 │           (route lookup + protocol wrap) │
 │                  |                       │
 │        [ Egress Priority Queue ]         │
 │             |          |                 │
 │  WiFi TX ──┘   LoRa TX (for commands)   │
 └──────────────────────────────────────────┘
                    |
              [MQTT publish]
                    |
                    v
          Pico W Dashboard (WiFi)
          subscribes to mesh/data/#
```

## Project Structure

```
pico_mpr_bridge/
├── main.py                  # Entry point: init hardware, start asyncio loop
├── config.py                # WiFi creds, MQTT broker, LoRa params, cost table
├── core/
│   ├── packet.py            # Common packet format, header parsing, fragmentation
│   ├── priority_queue.py    # Heapq-based priority queue
│   ├── neighbour.py         # Neighbour table management + Hello protocol
│   ├── router.py            # Dijkstra routing with cross-protocol cost model
│   ├── mpr.py               # MPR selection and election logic (OLSR-based)
│   └── translator.py        # Packet translation (LoRa↔MQTT, BLE↔MQTT)
├── interfaces/
│   ├── lora_interface.py    # LoRa RX/TX tasks, SX1262 wrapper
│   ├── ble_interface.py     # BLE central scan/read tasks via aioble
│   └── wifi_interface.py    # WiFi connection + MQTT pub/sub tasks
└── utils/
    ├── logger.py            # Simple serial logger with severity levels
    └── watchdog.py          # Software watchdog / heartbeat
```

## Required Libraries (upload to `/lib/` on Pico W)

| Library | Source |
|---------|--------|
| `sx127x.py` + `_sx127x.py` | [micropySX127x](https://github.com/ehong-tl/micropySX127x) |
| `aioble/` | Built into MicroPython v1.23+ or from [micropython-lib](https://github.com/micropython/micropython-lib/tree/master/micropython/bluetooth/aioble) |
| `umqtt/simple.py` + `robust.py` | [micropython-lib](https://github.com/micropython/micropython-lib/tree/master/micropython/umqtt.simple) |

## Quick Start

1. Flash MicroPython v1.23+ onto your Pico W
2. Upload required libraries to `/lib/` on the Pico W
3. Edit `config.py` with your WiFi SSID/password, MQTT broker IP, and LoRa pin mapping
4. Upload the entire `pico_mpr_bridge/` folder to the Pico W root
5. Reset the Pico W — `main.py` runs automatically

## Configuration

All tuneable parameters are in `config.py`:

- **`NODE_ID`** — Unique identifier for this node
- **`NODE_ROLE`** — `"bridge"`, `"sensor"`, or `"dashboard"`
- **`CAPABILITIES`** — List of active protocols: `["LoRa", "BLE", "WiFi", "MQTT"]`
- **`WIFI_SSID` / `WIFI_PASSWORD`** — WiFi credentials
- **`MQTT_BROKER`** — MQTT broker IP address
- **`LORA_FREQ`** — LoRa frequency (923.0 MHz for Singapore)
- **`HELLO_INTERVAL`** — Neighbour discovery interval in seconds
- **Cost model** — `COST_NATIVE`, `COST_LORA_WIFI`, `COST_BLE_WIFI`, `COST_LORA_BLE`

## MQTT Topics

| Topic | Purpose |
|-------|---------|
| `mesh/data/{node_id}` | Routine sensor telemetry |
| `mesh/alert/{node_id}` | Priority alerts (threshold exceeded) |
| `mesh/hello` | Neighbour discovery |
| `mesh/topology/{node_id}` | Topology broadcast for multi-bridge mesh |
| `mesh/cmd/{node_id}` | Commands from dashboard to sensor nodes |

## Key Algorithms

- **Dijkstra's Algorithm** with cross-protocol translation costs for optimal routing
- **OLSR-based MPR Selection** with protocol-aware tie-breaking for relay election
- **Priority Queue** (heapq) for packet scheduling — critical alerts always processed first
- **Self-Healing** — dead neighbour detection and automatic route recomputation

## Hardware Pin Mapping (Waveshare Pico-LoRa-SX1262)

| Function | Pico W GPIO |
|----------|-------------|
| SPI SCK  | GP10 |
| SPI MOSI | GP11 |
| SPI MISO | GP12 |
| NSS (CS) | GP3  |
| RESET    | GP15 |
| DIO1 (IRQ) | GP20 |
| BUSY     | GP2  |

## Scaling

- **N inputs**: Any LoRa/BLE sensor using the project sync word (`0x12`) or service UUID (`0xFFF0`) is auto-discovered
- **M outputs**: Any MQTT subscriber can listen to `mesh/data/#` or `mesh/alert/#`
- **Multi-bridge**: Deploy multiple bridges — topology is exchanged via MQTT and Dijkstra computes end-to-end paths
