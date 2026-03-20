#include <SPI.h>
#include <LoRa.h>
#include <SoftwareSerial.h>

#define LORA_FREQ 915E6
#define LORA_SF 9
#define LORA_BW 125E3
#define LORA_CR 5
#define LORA_SYNC_WORD 0x12
#define LORA_TX_POWER 17

#define LORA_CS 10
#define LORA_RESET 9
#define LORA_DIO0 2

// Dedicated UART bridge pins to Pico (avoids USB contention on D0/D1)
#define BRIDGE_TX_PIN 6
#define BRIDGE_RX_PIN 7
#define BRIDGE_BAUD 57600

#define STATUS_INTERVAL_MS 5000UL

unsigned long lastStatusMs = 0;
SoftwareSerial bridgeSerial(BRIDGE_RX_PIN, BRIDGE_TX_PIN);  // RX, TX


void sendBridgeLine(const String &line) {
  bridgeSerial.println(line);
  Serial.println(line);  // keep USB debug mirror
}


void handleTxCommandLine(const String &line) {
  if (line.startsWith("LORA_TX|")) {
    String txPayload = line.substring(8);
    if (txPayload.length() > 0) {
      LoRa.beginPacket();
      LoRa.print(txPayload);
      LoRa.endPacket();
    }
  }
}

void setupLoRa() {
  LoRa.setPins(LORA_CS, LORA_RESET, LORA_DIO0);

  if (!LoRa.begin(LORA_FREQ)) {
    sendBridgeLine("LORA_ERR|INIT|LoRa begin failed");
    while (1) {
      delay(1000);
    }
  }

  LoRa.setSpreadingFactor(LORA_SF);
  LoRa.setSignalBandwidth(LORA_BW);
  LoRa.setCodingRate4(LORA_CR);
  LoRa.setSyncWord(LORA_SYNC_WORD);
  LoRa.setTxPower(LORA_TX_POWER);
}

void setup() {
  Serial.begin(BRIDGE_BAUD);
  bridgeSerial.begin(BRIDGE_BAUD);
  delay(200);

  sendBridgeLine("LORA_STATUS|boot");
  setupLoRa();
  sendBridgeLine("LORA_STATUS|ready");
}

void loop() {
  int packetSize = LoRa.parsePacket();
  if (packetSize > 0) {
    String payload = "";
    while (LoRa.available()) {
      payload += (char)LoRa.read();
    }

    int rssi = LoRa.packetRssi();
    float snr = LoRa.packetSnr();

    String line = "LORA_RX|";
    line += String(rssi);
    line += "|";
    line += String(snr, 2);
    line += "|";
    line += payload;
    sendBridgeLine(line);
  }

  if (bridgeSerial.available()) {
    String line = bridgeSerial.readStringUntil('\n');
    line.trim();
    handleTxCommandLine(line);
  }

  // Optional: allow sending LORA_TX commands from USB serial monitor too.
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    handleTxCommandLine(line);
  }

  if (millis() - lastStatusMs >= STATUS_INTERVAL_MS) {
    sendBridgeLine("LORA_STATUS|alive");
    lastStatusMs = millis();
  }

  delay(10);
}
