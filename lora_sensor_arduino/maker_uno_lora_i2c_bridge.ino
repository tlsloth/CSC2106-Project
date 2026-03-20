#include <SPI.h>
#include <Wire.h>
#include <LoRa.h>

#define LORA_FREQ 915E6
#define LORA_SF 9
#define LORA_BW 125E3
#define LORA_CR 5
#define LORA_SYNC_WORD 0x12
#define LORA_TX_POWER 17

#define LORA_CS 10
#define LORA_RESET 9
#define LORA_DIO0 2

#define I2C_ADDR 0x42
#define STATUS_MAGIC 0xA5
#define PROTOCOL_VERSION 1

#define REG_STATUS 0x00
#define REG_RX_HEADER 0x01
#define REG_RX_DATA 0x02
#define REG_RX_POP 0x03
#define REG_TX_BEGIN 0x10
#define REG_TX_DATA 0x11
#define REG_TX_COMMIT 0x12

#define MAX_LORA_PAYLOAD 200
#define RX_QUEUE_DEPTH 4
#define I2C_CHUNK_SIZE 24
#define FLAG_HELLO 0x01

struct RxFrame {
  uint8_t length;
  int8_t rssi;
  int8_t snr_x4;
  uint8_t flags;
  uint8_t data[MAX_LORA_PAYLOAD];
};

RxFrame rxQueue[RX_QUEUE_DEPTH];
volatile uint8_t rxHead = 0;
volatile uint8_t rxTail = 0;
volatile uint8_t rxCount = 0;
volatile uint8_t droppedCount = 0;
volatile uint8_t lastError = 0;

uint8_t txBuffer[MAX_LORA_PAYLOAD];
volatile uint8_t txLength = 0;
volatile uint8_t txReceived = 0;
volatile bool txPending = false;

volatile uint8_t lastRegister = REG_STATUS;
volatile uint8_t lastOffset = 0;
volatile unsigned long i2cRequestCount = 0;
volatile unsigned long i2cReceiveCount = 0;

unsigned long lastHeartbeatMs = 0;

bool isHelloPayload(const uint8_t *data, uint8_t length) {
  const char needle[] = "\"type\":\"hello\"";
  uint8_t needleLen = sizeof(needle) - 1;

  if (length < needleLen) {
    return false;
  }

  for (uint8_t index = 0; index <= length - needleLen; index++) {
    bool match = true;
    for (uint8_t inner = 0; inner < needleLen; inner++) {
      if (data[index + inner] != (uint8_t)needle[inner]) {
        match = false;
        break;
      }
    }
    if (match) {
      return true;
    }
  }

  return false;
}

bool enqueueRxFrame(const uint8_t *data, uint8_t length, int rssi, float snr) {
  if (length == 0 || length > MAX_LORA_PAYLOAD) {
    lastError = 1;
    return false;
  }

  noInterrupts();
  if (rxCount >= RX_QUEUE_DEPTH) {
    droppedCount++;
    lastError = 2;
    interrupts();
    return false;
  }

  RxFrame &frame = rxQueue[rxHead];
  frame.length = length;
  frame.rssi = (int8_t)rssi;
  frame.snr_x4 = (int8_t)(snr * 4.0f);
  frame.flags = isHelloPayload(data, length) ? FLAG_HELLO : 0;
  for (uint8_t i = 0; i < length; i++) {
    frame.data[i] = data[i];
  }

  rxHead = (rxHead + 1) % RX_QUEUE_DEPTH;
  rxCount++;
  interrupts();
  return true;
}

void popRxFrame() {
  noInterrupts();
  if (rxCount > 0) {
    rxTail = (rxTail + 1) % RX_QUEUE_DEPTH;
    rxCount--;
  }
  interrupts();
}

void onI2CReceive(int count) {
  i2cReceiveCount++;
  if (count <= 0) {
    return;
  }

  uint8_t reg = (uint8_t)Wire.read();
  lastRegister = reg;

  switch (reg) {
    case REG_STATUS:
    case REG_RX_HEADER:
      break;

    case REG_RX_DATA:
      if (Wire.available()) {
        lastOffset = (uint8_t)Wire.read();
      }
      break;

    case REG_RX_POP:
      popRxFrame();
      break;

    case REG_TX_BEGIN:
      if (Wire.available()) {
        uint8_t requestedLength = (uint8_t)Wire.read();
        if (requestedLength == 0 || requestedLength > MAX_LORA_PAYLOAD) {
          txLength = 0;
          txReceived = 0;
          txPending = false;
          lastError = 3;
        } else {
          txLength = requestedLength;
          txReceived = 0;
          txPending = false;
          lastError = 0;
        }
      }
      break;

    case REG_TX_DATA:
      if (!Wire.available()) {
        lastError = 4;
        break;
      }
      {
        uint8_t offset = (uint8_t)Wire.read();
        uint8_t index = 0;
        while (Wire.available() && (offset + index) < MAX_LORA_PAYLOAD) {
          txBuffer[offset + index] = (uint8_t)Wire.read();
          index++;
        }
        if ((uint8_t)(offset + index) > txReceived) {
          txReceived = offset + index;
        }
      }
      break;

    case REG_TX_COMMIT:
      if (txLength > 0 && txReceived >= txLength) {
        txPending = true;
        lastError = 0;
      } else {
        lastError = 5;
      }
      break;

    default:
      lastError = 6;
      while (Wire.available()) {
        Wire.read();
      }
      break;
  }
}

void onI2CRequest() {
  i2cRequestCount++;
  switch (lastRegister) {
    case REG_STATUS: {
      uint8_t status[6] = {
        STATUS_MAGIC,
        PROTOCOL_VERSION,
        rxCount,
        txPending ? 1 : 0,
        droppedCount,
        lastError,
      };
      Wire.write(status, sizeof(status));
      break;
    }

    case REG_RX_HEADER: {
      uint8_t header[4] = {0, 0, 0, 0};
      if (rxCount > 0) {
        const RxFrame &frame = rxQueue[rxTail];
        header[0] = frame.length;
        header[1] = (uint8_t)frame.rssi;
        header[2] = (uint8_t)frame.snr_x4;
        header[3] = frame.flags;
      }
      Wire.write(header, sizeof(header));
      break;
    }

    case REG_RX_DATA: {
      if (rxCount > 0) {
        const RxFrame &frame = rxQueue[rxTail];
        if (lastOffset < frame.length) {
          uint8_t remaining = frame.length - lastOffset;
          uint8_t chunk = remaining > I2C_CHUNK_SIZE ? I2C_CHUNK_SIZE : remaining;
          Wire.write(frame.data + lastOffset, chunk);
        }
      }
      break;
    }

    default:
      Wire.write((uint8_t)0x00);
      break;
  }
}

void setupLoRa() {
  LoRa.setPins(LORA_CS, LORA_RESET, LORA_DIO0);
  if (!LoRa.begin(LORA_FREQ)) {
    Serial.println("ERROR: LoRa init failed");
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
  Serial.begin(115200);
  delay(200);

  Serial.println("=== Maker UNO LoRa I2C Bridge ===");
  setupLoRa();
  Serial.println("LoRa initialised");

  Wire.begin(I2C_ADDR);
  Wire.onReceive(onI2CReceive);
  Wire.onRequest(onI2CRequest);
  Serial.print("I2C slave ready at 0x");
  Serial.println(I2C_ADDR, HEX);
}

void loop() {
  int packetSize = LoRa.parsePacket();
  if (packetSize > 0) {
    uint8_t buffer[MAX_LORA_PAYLOAD];
    uint8_t length = 0;

    while (LoRa.available() && length < MAX_LORA_PAYLOAD) {
      buffer[length++] = (uint8_t)LoRa.read();
    }

    if (LoRa.available()) {
      while (LoRa.available()) {
        LoRa.read();
      }
      droppedCount++;
      lastError = 7;
    } else if (enqueueRxFrame(buffer, length, LoRa.packetRssi(), LoRa.packetSnr())) {
      Serial.print("[RX] len=");
      Serial.print(length);
      Serial.print(" RSSI=");
      Serial.print(LoRa.packetRssi());
      Serial.print(" SNR=");
      Serial.print(LoRa.packetSnr());
      Serial.print(" queue=");
      Serial.println(rxCount);
    }
  }

  if (txPending) {
    Serial.print("[TX] len=");
    Serial.println(txLength);
    LoRa.beginPacket();
    LoRa.write(txBuffer, txLength);
    LoRa.endPacket();
    txPending = false;
  }

  if (millis() - lastHeartbeatMs >= 5000UL) {
    Serial.print("[STATUS] queue=");
    Serial.print(rxCount);
    Serial.print(" dropped=");
    Serial.print(droppedCount);
    Serial.print(" txPending=");
    Serial.print(txPending ? 1 : 0);
    Serial.print(" i2cReq=");
    Serial.print(i2cRequestCount);
    Serial.print(" i2cRx=");
    Serial.println(i2cReceiveCount);
    lastHeartbeatMs = millis();
  }

  delay(20);
}