# Mosquitto + Dashboard Setup Guide

This guide documents the exact setup used in this project for:

- Arduino LoRa DHT sender -> Uno LoRa UART bridge
- Pico W bridge publisher -> Mosquitto
- Endpoint subscriber (MicroPython)
- Python web dashboard (Flask + Paho MQTT)

## 1. Data Flow

1. `lora_tx_DHT_sensor.ino` sends LoRa sensor payload (e.g. `T:31.0,H:66.3`)
2. `maker_uno_lora_uart_bridge.ino` receives and forwards over UART JSON
3. `lora_uart_bridge.py` on bridge Pico W publishes to MQTT:
   - Live topic: `mesh/data/{node}`
   - Retained topic: `mesh/latest/{node}`
4. Endpoint clients/dashboard subscribe to:
   - `mesh/data/+`
   - `mesh/latest/+`

## 2. Mosquitto Host Requirements (Windows)

Use the PC that runs Mosquitto as broker host.

### 2.1 Find broker IP

Run:

```powershell
ipconfig
```

Use the active adapter IPv4, e.g. `192.168.1.9`.

### 2.2 Configure mosquitto.conf

Use settings that allow LAN clients:

```conf
listener 1883 0.0.0.0
allow_anonymous true
# optional, default allows all:
# accept_protocol_versions 3,4,5
```

### 2.3 Verify listening address

Run:

```powershell
Get-NetTCPConnection -LocalPort 1883 -State Listen | Select-Object LocalAddress, LocalPort, OwningProcess
```

Expected `LocalAddress`:

- `0.0.0.0` (good), or
- your LAN IP (good)

Not acceptable for Pico clients:

- `127.0.0.1` only

### 2.4 Open firewall port

Run as Administrator:

```powershell
New-NetFirewallRule -DisplayName "Mosquitto 1883 Inbound" -Direction Inbound -Protocol TCP -LocalPort 1883 -Action Allow -Profile Any
```

### 2.5 Reachability test from another machine

```powershell
Test-NetConnection -ComputerName 192.168.1.9 -Port 1883
```

Expected: `TcpTestSucceeded : True`.

## 3. Project Files and Roles

- `pico_mpr_bridge/lora_uart_bridge.py`
  - Bridge Pico W script
  - Reads UART JSON and publishes MQTT live + retained

- `pico_mpr_bridge/mqtt_endpoint.py`
  - Endpoint Pico W subscriber script (serial text dashboard)

- `pico_mpr_bridge/python_dashboard.py`
  - PC-hosted web dashboard (Flask)

- `pico_mpr_bridge/requirements-dashboard.txt`
  - Python dependencies for web dashboard

## 4. Bridge Pico W Setup (Publisher)

In `pico_mpr_bridge/lora_uart_bridge.py`, set:

- `WIFI_SSID`
- `WIFI_PASSWORD`
- `MQTT_BROKER` (broker host LAN IP, e.g. `192.168.1.9`)
- `MQTT_PORT` (`1883`)

Expected bridge serial logs:

- `MQTT connected: ...`
- `MQTT TX: mesh/data/A ...`
- `MQTT TX(retained): mesh/latest/A ...`

## 5. Endpoint Pico W Setup (Subscriber)

In `pico_mpr_bridge/mqtt_endpoint.py`, set:

- `WIFI_SSID`
- `WIFI_PASSWORD`
- `MQTT_BROKER`
- `MQTT_PORT`

Expected endpoint logs:

- `MQTT connected: ...`
- `Subscribed to: mesh/data/+`
- `Subscribed to: mesh/latest/+`
- `MQTT RX: mesh/latest/A`
- `Parsed : node=A, T=..., H=..., rssi=...`

## 6. Python Web Dashboard Setup

From project root:

```powershell
pip install -r pico_mpr_bridge/requirements-dashboard.txt
python pico_mpr_bridge/python_dashboard.py
```

Open browser:

- `http://localhost:5050`

Health endpoint:

- `http://localhost:5050/api/health`

Nodes data endpoint:

- `http://localhost:5050/api/nodes`

## 7. Dashboard Time Fields

- `Last refresh`:
  - Browser polling time (UI fetch cycle)
  - Changes every polling interval

- `Last update`:
  - Time dashboard backend received latest MQTT message for that node
  - Changes only when new message arrives

## 8. Common Errors and Fixes

### Error: `Broker TCP check failed: [Errno 110] ETIMEDOUT`

Cause:

- Broker not reachable from client

Fix:

1. Verify broker IP in scripts
2. Ensure Mosquitto listens on `0.0.0.0:1883`
3. Open firewall TCP 1883
4. Verify with `Test-NetConnection`

### Error: `MQTT skipped or failed for node A`

Cause:

- Bridge publish path had no active client or reconnect issue

Fix:

- Use the updated `lora_uart_bridge.py` with auto-connect/reconnect in publish path

### Arduino sender shows `ACK wait timeout: no RX available`

Cause:

- No ACK returned by receiver; LoRa delivery may fail

Impact:

- No new payload reaches MQTT -> dashboard appears stale

Quick test:

- Temporarily set `REQUIRE_ACK false` in sender sketch to verify data path

## 9. Final End-to-End Checklist

1. Mosquitto listens on `0.0.0.0:1883`
2. Broker firewall allows inbound TCP 1883
3. `Test-NetConnection broker_ip -Port 1883` succeeds from client machines
4. Bridge Pico prints `MQTT TX` lines
5. Endpoint Pico receives `mesh/data/+` / `mesh/latest/+`
6. Web dashboard shows node card updates

## 10. Current Known Good MQTT Topics

- `mesh/data/A` (live)
- `mesh/latest/A` (retained snapshot)
- wildcard subscribers:
  - `mesh/data/+`
  - `mesh/latest/+`
