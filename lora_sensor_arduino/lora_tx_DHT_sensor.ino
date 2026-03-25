#include <SPI.h>
#include <Wire.h>
#include <RH_RF95.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <DHT.h>

// ============== LORA CONFIG ==============
#define RFM95_CS  10
#define RFM95_RST 9
#define RFM95_INT 2
#define RF95_FREQ 920.0

// ============== DHT CONFIG ==============
#define DHTPIN  3
#define DHTTYPE DHT22

// ============== OLED CONFIG ==============
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 32
#define OLED_RESET -1

// ============== NODE CONFIG ==============
#define MY_NODE_ID     'A'
#define TARGET_NODE_ID 'B'

// ============== PROTOCOL CONFIG ==============
#define MAX_RETRIES   3
#define ACK_TIMEOUT   3000
#define MSG_TYPE_DATA 0x01
#define MSG_TYPE_ACK  0x02
#define PACKET_START  0xCB
#define HEADER_SIZE   6
#define MAX_PAYLOAD   20

// ACK behavior controls:
// - Set REQUIRE_ACK to false to treat send as fire-and-forget.
// - Keep DEBUG_ACK enabled while validating receiver ACK format.
#define REQUIRE_ACK true
#define DEBUG_ACK   true

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

// ============== GLOBALS ==============
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);
RH_RF95 rf95(RFM95_CS, RFM95_INT);
DHT dht(DHTPIN, DHTTYPE);

uint8_t sequenceNum = 0;
int16_t packetCount = 0;
bool oledOK = false;

// ============== OLED ==============
void oledPrint(const char* msg) {
  if (!oledOK) return;
  display.clearDisplay();
  display.setCursor(0, 0);
  display.println(msg);
  display.display();
}

void oledStatus(float temp, float hum, const char* status) {
  if (!oledOK) return;
  display.clearDisplay();
  display.setCursor(0, 0);
  display.print(F("T:"));
  display.print(temp, 1);
  display.print(F("C H:"));
  display.print(hum, 1);
  display.println(F("%"));
  display.println(status);
  display.display();
}

// ============== CHECKSUM ==============
uint8_t calculateChecksum(Packet* pkt) {
  uint8_t cs = 0;
  cs ^= pkt->start;
  cs ^= pkt->from;
  cs ^= pkt->to;
  cs ^= pkt->type;
  cs ^= pkt->seq;
  cs ^= pkt->len;
  for (uint8_t i = 0; i < pkt->len; i++) {
    cs ^= pkt->payload[i];
  }
  return cs;
}

// ============== PACKET FUNCTIONS ==============
void createDataPacket(Packet* pkt, uint8_t targetId, const char* message) {
  pkt->start = PACKET_START;
  pkt->from  = MY_NODE_ID;
  pkt->to    = targetId;
  pkt->type  = MSG_TYPE_DATA;
  pkt->seq   = sequenceNum;
  pkt->len   = strlen(message);
  if (pkt->len > MAX_PAYLOAD) pkt->len = MAX_PAYLOAD;
  memcpy(pkt->payload, message, pkt->len);
  pkt->checksum = calculateChecksum(pkt);
}

uint8_t serializePacket(Packet* pkt, uint8_t* buffer) {
  uint8_t idx = 0;
  buffer[idx++] = pkt->start;
  buffer[idx++] = pkt->from;
  buffer[idx++] = pkt->to;
  buffer[idx++] = pkt->type;
  buffer[idx++] = pkt->seq;
  buffer[idx++] = pkt->len;
  for (uint8_t i = 0; i < pkt->len; i++) {
    buffer[idx++] = pkt->payload[i];
  }
  buffer[idx++] = pkt->checksum;
  return idx;
}

bool deserializePacket(uint8_t* buffer, uint8_t bufLen, Packet* pkt) {
  if (bufLen < HEADER_SIZE + 1) return false;
  if (buffer[0] != PACKET_START) return false;

  pkt->start = buffer[0];
  pkt->from  = buffer[1];
  pkt->to    = buffer[2];
  pkt->type  = buffer[3];
  pkt->seq   = buffer[4];
  pkt->len   = buffer[5];

  if (pkt->len > MAX_PAYLOAD) return false;
  if (bufLen < HEADER_SIZE + pkt->len + 1) return false;

  for (uint8_t i = 0; i < pkt->len; i++) {
    pkt->payload[i] = buffer[HEADER_SIZE + i];
  }
  pkt->checksum = buffer[HEADER_SIZE + pkt->len];

  return (calculateChecksum(pkt) == pkt->checksum);
}

void printHexBytes(const uint8_t* data, uint8_t len) {
  for (uint8_t i = 0; i < len; i++) {
    if (data[i] < 0x10) Serial.print('0');
    Serial.print(data[i], HEX);
    if (i + 1 < len) Serial.print(' ');
  }
}

bool isAckForTx(const Packet* ackPkt, const Packet* txPkt, uint8_t expectedFrom) {
  return (ackPkt->to   == MY_NODE_ID &&
          ackPkt->from == expectedFrom &&
          ackPkt->type == MSG_TYPE_ACK &&
          ackPkt->seq  == txPkt->seq);
}

// ============== SEND WITH RETRY ==============
bool sendWithRetry(uint8_t targetId, const char* message) {
  Packet txPacket;
  createDataPacket(&txPacket, targetId, message);

  uint8_t buffer[40];
  uint8_t len = serializePacket(&txPacket, buffer);

  Serial.print(F("Sending: "));
  Serial.println(message);

  for (uint8_t attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    Serial.print(F("Attempt "));
    Serial.print(attempt);
    Serial.print(F("/"));
    Serial.println(MAX_RETRIES);

    rf95.send(buffer, len);
    rf95.waitPacketSent();

    if (!REQUIRE_ACK) {
      Serial.println(F("ACK disabled: treating as delivered"));
      sequenceNum++;
      return true;
    }

    uint8_t recvBuf[50];
    uint8_t recvLen = sizeof(recvBuf);

    if (rf95.waitAvailableTimeout(ACK_TIMEOUT)) {
      if (rf95.recv(recvBuf, &recvLen)) {
        if (DEBUG_ACK) {
          Serial.print(F("RX candidate len="));
          Serial.print(recvLen);
          Serial.print(F(" bytes: "));
          printHexBytes(recvBuf, recvLen);
          Serial.println();
        }

        Packet ackPkt;
        if (deserializePacket(recvBuf, recvLen, &ackPkt)) {
          if (DEBUG_ACK) {
            Serial.print(F("ACK parsed: from="));
            Serial.print((char)ackPkt.from);
            Serial.print(F(" to="));
            Serial.print((char)ackPkt.to);
            Serial.print(F(" seq="));
            Serial.println(ackPkt.seq);
          }

          if (isAckForTx(&ackPkt, &txPacket, targetId)) {
            Serial.println(F("ACK OK!"));
            sequenceNum++;
            return true;
          } else if (DEBUG_ACK) {
            Serial.println(F("ACK parsed but does not match this TX (from/to/type/seq mismatch)"));
          }
        } else if (DEBUG_ACK) {
          Serial.println(F("RX frame is not valid custom ACK packet format"));
        }
      }
    } else if (DEBUG_ACK) {
      Serial.println(F("ACK wait timeout: no RX available"));
    }
    Serial.println(F("No ACK, retrying..."));
    delay(200 + random(200));
  }

  Serial.println(F("Delivery failed"));
  sequenceNum++;
  return false;
}

// ============== SETUP ==============
void setup() {
  Serial.begin(9600);
  delay(100);
  Serial.println(F("=== LoRa TX + DHT22 ==="));

  Wire.begin();
  dht.begin();

  // OLED init
  if (display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    oledOK = true;
  } else if (display.begin(SSD1306_SWITCHCAPVCC, 0x3D)) {
    oledOK = true;
  }
  if (oledOK) {
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.clearDisplay();
    display.display();
    oledPrint("Init...");
  }

  // LoRa init
  pinMode(RFM95_RST, OUTPUT);
  digitalWrite(RFM95_RST, HIGH);
  delay(10);
  digitalWrite(RFM95_RST, LOW);
  delay(10);
  digitalWrite(RFM95_RST, HIGH);
  delay(10);

  if (!rf95.init()) {
    Serial.println(F("LoRa FAILED"));
    while (1);
  }
  if (!rf95.setFrequency(RF95_FREQ)) {
    Serial.println(F("Freq FAILED"));
    while (1);
  }
  rf95.setTxPower(13, false);

  Serial.println(F("Setup OK!"));
  oledPrint("Ready");
  delay(1000);
}

// ============== LOOP ==============
void loop() {
  // Read DHT22
  float humidity    = dht.readHumidity();
  float temperature = dht.readTemperature();  // Celsius

  // Check if read failed
  if (isnan(humidity) || isnan(temperature)) {
    Serial.println(F("DHT22 read failed!"));
    delay(2000);
    return;
  }

  Serial.print(F("Temp: "));
  Serial.print(temperature, 1);
  Serial.print(F("C  Hum: "));
  Serial.print(humidity, 1);
  Serial.println(F("%"));

  // Format payload: "T:24.5,H:61.2" (fits in 20 bytes)
  char message[MAX_PAYLOAD];
  dtostrf(temperature, 4, 1, message);           // e.g. "24.5"
  char humStr[8];
  dtostrf(humidity, 4, 1, humStr);               // e.g. "61.2"
  
  // Build "T:24.5,H:61.2"
  char payload[MAX_PAYLOAD];
  snprintf(payload, MAX_PAYLOAD, "T:%s,H:%s", message, humStr);

  Serial.print(F("Payload: "));
  Serial.println(payload);

  oledStatus(temperature, humidity, "Sending...");

  bool ok = sendWithRetry(TARGET_NODE_ID, payload);

  if (ok) {
    Serial.println(F("Delivered!"));
    oledStatus(temperature, humidity, "Delivered!");
  } else {
    oledStatus(temperature, humidity, "Failed!");
  }

  // DHT22 needs minimum 2s between reads
  delay(5000);
}
