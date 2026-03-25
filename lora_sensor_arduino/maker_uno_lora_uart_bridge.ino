#include <SPI.h>
#include <RH_RF95.h>
#include <SoftwareSerial.h>

// LoRa Shield Pins
#define RFM95_CS    10
#define RFM95_RST    9
#define RFM95_INT    2
#define RF95_FREQ  920.0

// UART to Pico W
#define PICO_RX_PIN  7    // Arduino RX <- Pico TX
#define PICO_TX_PIN  6    // Arduino TX -> Pico RX (use level shifting)
#define PICO_BAUD    9600

// Protocol
#define PACKET_START  0xCB
#define MY_NODE_ID    'B'
#define MSG_TYPE_DATA 0x01
#define MSG_TYPE_ACK  0x02
#define HEADER_SIZE    6
#define MAX_PAYLOAD   20

struct Packet {
  uint8_t start;
  uint8_t from;
  uint8_t to;
  uint8_t type;
  uint8_t seq;
  uint8_t len;
  uint8_t payload[MAX_PAYLOAD];
  uint8_t checksum;
};

RH_RF95 rf95(RFM95_CS, RFM95_INT);
SoftwareSerial picoSerial(PICO_RX_PIN, PICO_TX_PIN);

uint8_t calculateChecksum(Packet *pkt) {
  uint8_t cs = pkt->start ^ pkt->from ^ pkt->to ^ pkt->type ^ pkt->seq ^ pkt->len;
  for (uint8_t i = 0; i < pkt->len; i++) {
    cs ^= pkt->payload[i];
  }
  return cs;
}

bool deserializePacket(uint8_t *buf, uint8_t bufLen, Packet *pkt) {
  if (bufLen < HEADER_SIZE + 1) return false;
  if (buf[0] != PACKET_START) return false;

  pkt->start = buf[0];
  pkt->from = buf[1];
  pkt->to = buf[2];
  pkt->type = buf[3];
  pkt->seq = buf[4];
  pkt->len = buf[5];

  if (pkt->len > MAX_PAYLOAD) return false;
  if (bufLen < HEADER_SIZE + pkt->len + 1) return false;

  for (uint8_t i = 0; i < pkt->len; i++) {
    pkt->payload[i] = buf[HEADER_SIZE + i];
  }
  pkt->checksum = buf[HEADER_SIZE + pkt->len];

  return (calculateChecksum(pkt) == pkt->checksum);
}

void sendAck(Packet *rxPkt) {
  Packet ack;
  ack.start = PACKET_START;
  ack.from = MY_NODE_ID;
  ack.to = rxPkt->from;
  ack.type = MSG_TYPE_ACK;
  ack.seq = rxPkt->seq;
  ack.len = 0;
  ack.checksum = calculateChecksum(&ack);

  uint8_t buf[7] = {
    ack.start, ack.from, ack.to,
    ack.type, ack.seq, ack.len, ack.checksum
  };

  rf95.send(buf, sizeof(buf));
  rf95.waitPacketSent();
  Serial.println(F("ACK sent"));
}

void setup() {
  Serial.begin(PICO_BAUD);
  picoSerial.begin(PICO_BAUD);
  delay(100);
  Serial.println(F("=== LoRa-UART Bridge (RH_RF95) ==="));

  pinMode(RFM95_RST, OUTPUT);
  digitalWrite(RFM95_RST, HIGH);
  delay(10);
  digitalWrite(RFM95_RST, LOW);
  delay(10);
  digitalWrite(RFM95_RST, HIGH);
  delay(10);

  if (!rf95.init()) {
    Serial.println(F("LoRa FAILED"));
    while (1) {}
  }

  if (!rf95.setFrequency(RF95_FREQ)) {
    Serial.println(F("Freq FAILED"));
    while (1) {}
  }

  rf95.setTxPower(13, false);
  Serial.println(F("Ready - listening for LoRa..."));
}

void loop() {
  if (rf95.available()) {
    uint8_t buf[50];
    uint8_t len = sizeof(buf);

    if (rf95.recv(buf, &len)) {
      int16_t rssi = rf95.lastRssi();
      Packet pkt;

      if (deserializePacket(buf, len, &pkt)) {
        if (pkt.type == MSG_TYPE_DATA && (pkt.to == MY_NODE_ID || pkt.to == 0xFF)) {
          sendAck(&pkt);

          char raw[MAX_PAYLOAD + 1];
          memcpy(raw, pkt.payload, pkt.len);
          raw[pkt.len] = '\0';

          // JSON line expected by pico_mpr_bridge/interfaces/uart_lora_interface.py
          char json[96];
          snprintf(
            json,
            sizeof(json),
            "{\"raw\":\"%s\",\"node\":\"%c\",\"rssi\":%d}",
            raw,
            (char)pkt.from,
            (int)rssi
          );

          picoSerial.println(json);
          Serial.print(F("Forwarded: "));
          Serial.println(json);
        }
      } else {
        Serial.println(F("Bad packet - dropped"));
      }
    }
  }
}
