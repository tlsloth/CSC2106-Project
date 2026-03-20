/*
 * LoRa Sensor Node for Arduino (Maker Uno)
 * Tests communication with MPR Bridge (Pico W)
 * 
 * Hardware: Maker Uno + SX1276 RFM95W LoRa Shield (915 MHz)
 * Library: LoRa by Sandeep Mistry (install via Arduino Library Manager)
 * 
 * Pin Mapping (adjust based on your shield):
 * - NSS (CS): Pin 10
 * - DIO0: Pin 2
 * - RESET: Pin 9
 * - SPI uses standard Arduino pins (MOSI=11, MISO=12, SCK=13)
 */

#include <SPI.h>
#include <LoRa.h>

// LoRa Configuration
#define LORA_FREQ 915E6       // 915 MHz (US band)
#define LORA_SF 7             // Spreading Factor
#define LORA_BW 125E3         // Bandwidth 125 kHz
#define LORA_CR 5             // Coding rate 4/5
#define LORA_SYNC_WORD 0x12   // Must match bridge
#define LORA_TX_POWER 17      // dBm

// Pin Configuration (standard LoRa shield for Arduino)
#define LORA_CS 10
#define LORA_RESET 9
#define LORA_DIO0 2

// Node Identity
const char* NODE_ID = "arduino_sensor_01";
unsigned int msg_seq = 0;

// Timing
unsigned long lastHelloTime = 0;
unsigned long lastDataTime = 0;
const unsigned long HELLO_INTERVAL = 5000;  // 5 seconds
const unsigned long DATA_INTERVAL = 10000;   // 10 seconds

void setup() {
  Serial.begin(115200);
  while (!Serial);
  
  Serial.println("=== LoRa Sensor Node (Arduino) ===");
  Serial.print("Node ID: ");
  Serial.println(NODE_ID);
  
  // Initialize LoRa
  LoRa.setPins(LORA_CS, LORA_RESET, LORA_DIO0);
  
  if (!LoRa.begin(LORA_FREQ)) {
    Serial.println("ERROR: LoRa init failed!");
    while (1);
  }
  
  // Configure LoRa parameters
  LoRa.setSpreadingFactor(LORA_SF);
  LoRa.setSignalBandwidth(LORA_BW);
  LoRa.setCodingRate4(LORA_CR);
  LoRa.setSyncWord(LORA_SYNC_WORD);
  LoRa.setTxPower(LORA_TX_POWER);
  
  Serial.println("LoRa initialized successfully!");
  Serial.print("Frequency: ");
  Serial.print(LORA_FREQ / 1E6);
  Serial.println(" MHz");
  Serial.print("Spreading Factor: ");
  Serial.println(LORA_SF);
  Serial.println("\nSending Hello + Data packets...\n");
}

void loop() {
  unsigned long now = millis();
  
  // Send Hello packet every 5 seconds
  if (now - lastHelloTime >= HELLO_INTERVAL) {
    sendHelloPacket();
    lastHelloTime = now;
  }
  
  // Send Data packet every 10 seconds
  if (now - lastDataTime >= DATA_INTERVAL) {
    sendDataPacket();
    lastDataTime = now;
  }
  
  // Check for incoming packets
  int packetSize = LoRa.parsePacket();
  if (packetSize) {
    handleIncomingPacket(packetSize);
  }
  
  delay(100);
}

void sendHelloPacket() {
  // Hello packet format: {"type":"hello","src":"arduino_sensor_01","seq":123}
  String payload = "{\"type\":\"hello\",\"src\":\"";
  payload += NODE_ID;
  payload += "\",\"seq\":";
  payload += msg_seq++;
  payload += "}";
  
  LoRa.beginPacket();
  LoRa.print(payload);
  LoRa.endPacket();
  
  Serial.print("[TX HELLO] ");
  Serial.println(payload);
}

void sendDataPacket() {
  // Simulate sensor readings
  float temperature = 25.0 + (random(0, 100) / 10.0);  // 25-35°C
  int humidity = 50 + random(0, 30);                    // 50-80%
  int distance = 20 + random(0, 50);                    // 20-70 cm
  
  // Data packet format matching bridge expectations
  // {"src":"arduino_sensor_01","dst":"mqtt_broker","hop_src":"arduino_sensor_01",
  //  "hop_dst":"bridge","ttl":5,"priority":5,"seq":124,
  //  "payload":{"temp":28.5,"humidity":65,"distance":35}}
  
  String packet = "{\"src\":\"";
  packet += NODE_ID;
  packet += "\",\"dst\":\"mqtt_broker\",\"hop_src\":\"";
  packet += NODE_ID;
  packet += "\",\"hop_dst\":\"bridge\",\"ttl\":5,\"priority\":5,\"seq\":";
  packet += msg_seq++;
  packet += ",\"payload\":{\"temp\":";
  packet += temperature;
  packet += ",\"humidity\":";
  packet += humidity;
  packet += ",\"distance\":";
  packet += distance;
  packet += "}}";
  
  LoRa.beginPacket();
  LoRa.print(packet);
  LoRa.endPacket();
  
  Serial.print("[TX DATA] ");
  Serial.print("Temp=");
  Serial.print(temperature);
  Serial.print("°C, Humidity=");
  Serial.print(humidity);
  Serial.print("%, Distance=");
  Serial.print(distance);
  Serial.print("cm, Seq=");
  Serial.println(msg_seq - 1);
}

void handleIncomingPacket(int packetSize) {
  String incoming = "";
  
  while (LoRa.available()) {
    incoming += (char)LoRa.read();
  }
  
  int rssi = LoRa.packetRssi();
  float snr = LoRa.packetSnr();
  
  Serial.print("[RX] ");
  Serial.print(incoming);
  Serial.print(" | RSSI: ");
  Serial.print(rssi);
  Serial.print(" dBm, SNR: ");
  Serial.print(snr);
  Serial.println(" dB");
  
  // Check if it's an ACK or command from bridge
  if (incoming.indexOf("\"type\":\"ack\"") > 0) {
    Serial.println("-> ACK received from bridge!");
  } else if (incoming.indexOf("\"type\":\"hello\"") > 0) {
    Serial.println("-> Hello received from bridge!");
  }
}
