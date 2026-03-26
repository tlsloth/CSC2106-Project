# MQTT Setup Guide (New User, End-to-End)

This guide tells you exactly which files to run on each device and in what order.

## 1. What You Are Building

Data path:

1. Arduino sensor sends LoRa payload (example: T:31.0,H:66.3)
2. Arduino UNO LoRa-UART bridge receives LoRa, sends JSON over UART to Pico W
3. Pico W bridge runs MPR app, publishes to Mosquitto
4. Dashboard app reads MQTT and shows node values

## 2. File Selection (Use These Files)

### Arduino sensor node (transmitter)

- lora_sensor_arduino/lora_tx_DHT_sensor.ino

### Arduino UNO LoRa-UART bridge (receiver + ACK + UART forward)

- lora_sensor_arduino/maker_uno_lora_uart_bridge.ino

### Pico W bridge (recommended app path)

- pico_mpr_bridge/main.py
- pico_mpr_bridge/config.py
- pico_mpr_bridge/core/*
- pico_mpr_bridge/interfaces/*
- pico_mpr_bridge/utils/*
- pico_mpr_bridge/lib/umqtt/* and pico_mpr_bridge/lib/aioble/* as needed

### Optional Pico W MQTT serial subscriber (debug only)

- pico_mpr_bridge/mqtt_endpoint.py

### PC web dashboard

- pico_mpr_bridge/python_dashboard.py
- pico_mpr_bridge/requirements-dashboard.txt

Important:

- Do not run pico_mpr_bridge/lora_uart_bridge.py together with pico_mpr_bridge/main.py on the same Pico. Choose one runtime path.
- Recommended runtime path is main.py.

## 3. Network and Broker Setup (Windows)

Run on the Mosquitto host PC.

### 3.1 Find broker IP

```powershell
ipconfig
```

Use the active adapter IPv4, for example 192.168.1.9.

### 3.2 Configure mosquitto.conf (just add on any line)

```conf
listener 1883 0.0.0.0
allow_anonymous true
```

### 3.3 Verify listener

```powershell
Get-NetTCPConnection -LocalPort 1883 -State Listen | Select-Object LocalAddress, LocalPort, OwningProcess
```

Expected LocalAddress is 0.0.0.0 or LAN IPv4, not only 127.0.0.1.

### 3.4 Open firewall

```powershell
New-NetFirewallRule -DisplayName "Mosquitto 1883 Inbound" -Direction Inbound -Protocol TCP -LocalPort 1883 -Action Allow -Profile Any
```

### 3.5 Connectivity check

```powershell
Test-NetConnection -ComputerName 192.168.1.9 -Port 1883
```

Expected: TcpTestSucceeded : True

## 4. Arduino Setup

### 4.1 Upload sender sketch

1. Open lora_sensor_arduino/lora_tx_DHT_sensor.ino
2. Select board and COM port
3. Upload
4. Open serial monitor at 9600

Expected sender logs include:

- Sending: T:xx.x,H:yy.y
- Attempt 1/3
- ACK OK! (ideal)

### 4.2 Upload UNO LoRa-UART bridge sketch

1. Open lora_sensor_arduino/maker_uno_lora_uart_bridge.ino
2. Select board and COM port
3. Upload
4. Open serial monitor at 9600

Expected bridge logs include:

- Ready - listening for LoRa...
- ACK sent
- Forwarded: {"raw":"T:..,H:..","node":"A","rssi":...}

## 5. Pico W Bridge Setup (main.py path)

### 5.1 Update config

Edit pico_mpr_bridge/config.py:

- WIFI_SSID
- WIFI_PASSWORD
- MQTT_BROKER (PC broker IPv4)
- MQTT_PORT
- LORA_TRANSPORT = "UART"
- UART_LORA_ID / UART_LORA_TX_PIN / UART_LORA_RX_PIN

### 5.2 Upload files to Pico W

Upload the whole pico_mpr_bridge folder content needed by main.py.

### 5.3 Run and verify

Reset Pico W (main.py auto-runs).

Expected serial logs include:

- Interfaces — LoRa(UART):OK ...
- LoRa-over-UART RX task started
- UART RX: {"raw":"T:..,H:..","node":"A","rssi":...}
- Published to mesh/data/A ...

## 6. PC Dashboard Setup

From repository root:

```powershell
pip install -r pico_mpr_bridge/requirements-dashboard.txt
python pico_mpr_bridge/python_dashboard.py
```

Open:

- http://localhost:5050

Useful APIs:

- http://localhost:5050/api/health
- http://localhost:5050/api/nodes

## 7. MQTT Topics Used

Published by bridge:

- mesh/data/{node_id}
- mesh/latest/{node_id} (retained)

Subscribed by dashboard:

- mesh/data/+
- mesh/latest/+

## 8. Message Format Notes

The dashboard now supports all of these payload styles:

1. Legacy format

```json
{"node":"A","T":31.2,"H":65.0,"rssi":-45}
```

2. Standard MPR format

```json
{"src":"A","dst":"dashboard","data":{"temp":31.2,"humidity":65.0}}
```

3. Raw sensor text wrapped in JSON

```json
{"src":"A","data":{"raw":"T:31.2,H:65.0"}}
```

## 9. ACK vs MQTT Clarification

- ACK is sender-side confirmation and can fail independently.
- MQTT update can still succeed even if sender reports ACK timeout.
- If you see dashboard updates, the forward path is working.

### Problem: Sender often says no RX available

Check:

1. UNO bridge serial shows ACK sent
2. Sender and bridge frequency and packet format match
3. Antenna and distance are reasonable
4. Keep DEBUG_ACK enabled in lora_tx_DHT_sensor.ino while testing

### Problem: Pico cannot publish MQTT

Check:

1. MQTT_BROKER in pico_mpr_bridge/config.py is your active laptop IPv4
2. Mosquitto bound to 0.0.0.0:1883
3. Firewall rule for TCP 1883 exists

