# LoRa Sensor Node for Arduino (Maker Uno)

Test sensor for communicating with the Pico W MPR Bridge via LoRa.

## Hardware Requirements

- **Arduino Maker Uno** (or compatible Arduino board)
- **SX1276 RFM95W LoRa Shield** (915 MHz)
- **USB cable** for programming and serial monitor

## Pin Connections

Standard LoRa shield for Arduino uses:

| Signal | Arduino Pin | LoRa Module |
|--------|-------------|-------------|
| CS/NSS | 10 | CS |
| RESET | 9 | RESET |
| DIO0 | 2 | DIO0 |
| MOSI | 11 | MOSI |
| MISO | 12 | MISO |
| SCK | 13 | SCK |

**Note:** If your shield uses different pins, modify the `#define` statements at the top of the sketch.

## Setup Instructions

### 1. Install Arduino IDE
Download from [arduino.cc](https://www.arduino.cc/en/software)

### 2. Install LoRa Library
1. Open Arduino IDE
2. Go to **Sketch → Include Library → Manage Libraries**
3. Search for **"LoRa"** by Sandeep Mistry
4. Click **Install**

### 3. Configure Board
1. Connect Maker Uno via USB
2. Select **Tools → Board → Arduino Uno**
3. Select correct **Tools → Port** (COM port)

### 4. Upload Sketch
1. Open `lora_sensor_arduino.ino`
2. Click **Upload** button (→)
3. Wait for "Done uploading"

### 5. Test Communication
1. Open **Tools → Serial Monitor**
2. Set baud rate to **115200**
3. You should see:
   ```
   === LoRa Sensor Node (Arduino) ===
   Node ID: arduino_sensor_01
   LoRa initialized successfully!
   Frequency: 915.0 MHz
   Spreading Factor: 7
   
   [TX HELLO] {"type":"hello","src":"arduino_sensor_01","seq":0}
   [TX DATA] Temp=28.3°C, Humidity=65%, Distance=42cm, Seq=1
   ```

## Expected Behavior

**Arduino sends:**
- **Hello packets** every 5 seconds (neighbor discovery)
- **Data packets** every 10 seconds (sensor telemetry)

**Bridge receives:**
- LoRa packets from Arduino
- Routes them to MQTT broker
- Publishes to `mesh/data/arduino_sensor_01` topic

**You should see on bridge serial log:**
```
[LoRa RX] Neighbour discovered: arduino_sensor_01
[Translator] Routing LoRa→WiFi: seq=1
[WiFi TX] Published to mesh/data/arduino_sensor_01
```

## Troubleshooting

### LoRa init failed
- Check shield is properly seated on Arduino
- Verify pin connections match sketch
- Try different CS/RESET/DIO0 pins if using custom wiring

### No packets received on bridge
- Verify both use **915 MHz** frequency
- Check **LORA_SYNC_WORD** matches bridge config (0x12)
- Ensure spreading factor (SF7) and bandwidth (125 kHz) match
- Use shorter distance (<100m outdoors, <20m indoors) for initial test

### Bridge not publishing to MQTT
- Check bridge WiFi connection
- Verify MQTT broker is running
- Subscribe to `mesh/data/#` to see all topics

## Customization

Edit these values in the sketch:

```cpp
#define LORA_FREQ 915E6       // Match your region (868E6 for EU)
#define LORA_SF 7             // Spreading factor (7-12, higher = longer range)
const char* NODE_ID = "arduino_sensor_01";  // Unique node identifier
const unsigned long DATA_INTERVAL = 10000;  // Send interval in ms
```

## Testing Checklist

- [ ] Arduino serial monitor shows TX messages
- [ ] Bridge serial log shows LoRa RX from arduino_sensor_01
- [ ] MQTT broker receives messages on mesh/data/arduino_sensor_01
- [ ] Dashboard displays sensor data (temp, humidity, distance)
