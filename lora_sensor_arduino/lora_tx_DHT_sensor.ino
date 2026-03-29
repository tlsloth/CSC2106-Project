#include <SPI.h>
#include <Wire.h>
#include <RH_RF95.h>
#include <DHT.h>

// Set to 1 only when OLED is required and flash budget allows it on your board.
#define ENABLE_OLED 0

#if ENABLE_OLED
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#endif

// SENDER NODE, EDGE DEVICE
// ============== LORA CONFIG ==============
#define RFM95_CS 10
#define RFM95_RST 9
#define RFM95_INT 2
#define RF95_FREQ 920.0

// ============== DHT CONFIG ==============
#define DHTPIN 3
#define DHTTYPE DHT22

// ============== OLED CONFIG ==============
#if ENABLE_OLED
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 32
#define OLED_RESET -1
#endif

// ============== NODE CONFIG ==============
#define MY_NODE_ID 'A'
#define TARGET_NODE_ID 'B'

// Mesh join/discovery control channel (JSON over LoRa)
const char *MESH_NODE_ID = "dht_sensor_A";
const char *MESH_NETWORK_NAME = "CSC2106_MESH";
const char *MESH_JOIN_KEY = "mesh_key_v1";

// ============== PROTOCOL CONFIG ==============
#define MAX_RETRIES 3
#define ACK_TIMEOUT 3000
#define ACK_RX_GUARD_MS 15
#define MSG_TYPE_DATA 0x01
#define MSG_TYPE_ACK 0x02
#define PACKET_START 0xCB
#define HEADER_SIZE 6
#define MAX_PAYLOAD 20

// ACK behavior controls:
// - Set REQUIRE_ACK to false to treat send as fire-and-forget.
// - Keep DEBUG_ACK enabled while validating receiver ACK format.
#define REQUIRE_ACK true
#define DEBUG_ACK false

struct Packet
{
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
#if ENABLE_OLED
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);
#endif
RH_RF95 rf95(RFM95_CS, RFM95_INT);
DHT dht(DHTPIN, DHTTYPE);

uint8_t sequenceNum = 0;
int16_t packetCount = 0;
bool oledOK = false;
bool joined = false;
String learned_bridge_id = "bridge_01";
unsigned long lastJoinTime = 0;
unsigned long lastHelloTime = 0;
unsigned long lastRouteQueryTime = 0;
unsigned long lastJoinWaitLogTime = 0;

const unsigned long JOIN_INTERVAL = 10000;
const unsigned long HELLO_INTERVAL = 5000;
const unsigned long ROUTE_QUERY_INTERVAL = 15000;
const unsigned long JOIN_WAIT_LOG_INTERVAL = 1000;

// ===== Original String-based helpers (kept for now if you still use them elsewhere) =====
int findJsonValueStart(const String &json, const char *key)
{
  String needle = String("\"") + key + "\"";
  int keyIndex = json.indexOf(needle);
  if (keyIndex < 0)
  {
    return -1;
  }

  int colon = json.indexOf(':', keyIndex + needle.length());
  if (colon < 0)
  {
    return -1;
  }

  int start = colon + 1;
  while (start < json.length())
  {
    char c = json[start];
    if (c == ' ' || c == '\t' || c == '\r' || c == '\n')
    {
      start++;
      continue;
    }
    break;
  }
  return start;
}

String extractJsonString(const String &json, const char *key)
{
  int start = findJsonValueStart(json, key);
  if (start < 0 || start >= json.length() || json[start] != '"')
  {
    return "";
  }

  start++;
  int end = json.indexOf('"', start);
  if (end < 0)
  {
    return "";
  }
  return json.substring(start, end);
}

bool extractJsonBool(const String &json, const char *key)
{
  int start = findJsonValueStart(json, key);
  if (start < 0)
  {
    return false;
  }

  return json.startsWith("true", start) || json.startsWith("1", start);
}

// ===== New C-string-based JSON helpers =====
int findJsonValueStart(const char *json, const char *key)
{
  if (!json || !key) return -1;

  // Build `"key"`
  char needle[64];
  snprintf(needle, sizeof(needle), "\"%s\"", key);

  const char *keyPos = strstr(json, needle);
  if (!keyPos) return -1;

  const char *colon = strchr(keyPos + strlen(needle), ':');
  if (!colon) return -1;

  const char *p = colon + 1;
  while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n')
  {
    p++;
  }
  return (int)(p - json);
}

bool extractJsonBool(const char *json, const char *key)
{
  int start = findJsonValueStart(json, key);
  if (start < 0) return false;

  const char *p = json + start;
  return (strncmp(p, "true", 4) == 0) || (*p == '1');
}

bool extractJsonString(const char *json, const char *key,
                       char *out, size_t outSize)
{
  if (!json || !key || !out || outSize == 0)
  {
    return false;
  }

  // Build `"key"`
  char needle[64];
  snprintf(needle, sizeof(needle), "\"%s\"", key);

  const char *keyPos = strstr(json, needle);
  if (!keyPos)
  {
    out[0] = '\0';
    return false;
  }

  const char *colon = strchr(keyPos + strlen(needle), ':');
  if (!colon)
  {
    out[0] = '\0';
    return false;
  }

  const char *p = colon + 1;
  while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n')
  {
    p++;
  }

  if (*p != '"')
  {
    out[0] = '\0';
    return false;
  }
  p++;

  const char *end = strchr(p, '"');
  if (!end)
  {
    out[0] = '\0';
    return false;
  }

  size_t len = (size_t)(end - p);
  if (len >= outSize)
  {
    len = outSize - 1;
  }
  memcpy(out, p, len);
  out[len] = '\0';
  return true;
}

// ============== LORA SEND (raw JSON) ==============
void sendRawLoRa(const char *payload)
{
  if (!payload)
  {
    return;
  }

  size_t payloadLen = strlen(payload);
  if (payloadLen == 0)
  {
    Serial.println(F("[TX WARN] empty payload"));
    return;
  }

  rf95.send((const uint8_t *)payload, payloadLen);
  rf95.waitPacketSent();
  rf95.setModeRx();
}

void sendJoinRequest()
{
  char payload[200];
  uint8_t seq = sequenceNum++;
  int written = snprintf(
      payload,
      sizeof(payload),
      "{\"type\":\"join_req\",\"node_id\":\"%s\",\"network\":\"%s\",\"auth\":\"%s\",\"capabilities\":[\"LoRa\"],\"seq\":%u}",
      MESH_NODE_ID,
      MESH_NETWORK_NAME,
      MESH_JOIN_KEY,
      (unsigned int)seq);

  Serial.print(F("[TX JOIN] "));
  if (written <= 0 || written >= (int)sizeof(payload))
  {
    Serial.print(F("build failed, written="));
    Serial.println(written);
    return;
  }

  Serial.print(F("len="));
  Serial.print(strlen(payload));
  Serial.print(F(" payload="));
  Serial.println(payload);
  sendRawLoRa(payload);
}

void sendHelloPacket()
{
  char payload[200];
  uint8_t seq = sequenceNum++;
  int written = snprintf(
      payload,
      sizeof(payload),
      "{\"type\":\"hello\",\"node_id\":\"%s\",\"role\":\"sensor\",\"capabilities\":[\"LoRa\"],\"network\":\"%s\",\"seq\":%u}",
      MESH_NODE_ID,
      MESH_NETWORK_NAME,
      (unsigned int)seq);

  Serial.print(F("[TX HELLO] "));
  if (written <= 0 || written >= (int)sizeof(payload))
  {
    Serial.print(F("build failed, written="));
    Serial.println(written);
    return;
  }

  Serial.print(F("len="));
  Serial.print(strlen(payload));
  Serial.print(F(" payload="));
  Serial.println(payload);
  sendRawLoRa(payload);
}

void sendRouteQuery(const char *dst)
{
  char payload[200];
  uint8_t seq = sequenceNum++;
  int written = snprintf(
      payload,
      sizeof(payload),
      "{\"type\":\"route_query\",\"src\":\"%s\",\"dst\":\"%s\",\"seq\":%u}",
      MESH_NODE_ID,
      dst,
      (unsigned int)seq);

  Serial.print(F("[TX ROUTE_QUERY] "));
  if (written <= 0 || written >= (int)sizeof(payload))
  {
    Serial.print(F("build failed, written="));
    Serial.println(written);
    return;
  }

  Serial.print(F("len="));
  Serial.print(strlen(payload));
  Serial.print(F(" payload="));
  Serial.println(payload);
  sendRawLoRa(payload);
}

// ============== CONTROL MESSAGE HANDLER (C-string) ==============
void handleControlMessage(const char *incoming)
{
  if (!incoming || incoming[0] == '\0')
  {
    Serial.println(F("[CTRL] empty incoming"));
    return;
  }

  Serial.print(F("[CTRL] incoming len="));
  Serial.println(strlen(incoming));
  Serial.print(F("[CTRL] incoming="));
  Serial.println(incoming);

  if (strstr(incoming, "\"type\":\"join_ack\"") != nullptr)
  {
    Serial.println(F("[CTRL] Found join ack!"));

    if (extractJsonBool(incoming, "accepted"))
    {
      char bridgeId[32] = {0};
      extractJsonString(incoming, "bridge_id", bridgeId, sizeof(bridgeId));

      if (bridgeId[0] != '\0')
      {
        learned_bridge_id = bridgeId;   // still stored in a String for convenience
      }

      joined = true;
      lastRouteQueryTime = 0;

      Serial.print(F("Join accepted, bridge="));
      Serial.println(bridgeId);
      oledPrint("Joined mesh");
    }
    else
    {
      char reason[32] = {0};
      extractJsonString(incoming, "reason", reason, sizeof(reason));

      if (reason[0] == '\0')
      {
        strncpy(reason, "join_not_accepted", sizeof(reason) - 1);
      }

      joined = false;
      Serial.print(F("Join rejected: "));
      Serial.println(reason);
    }
  }
  else if (strstr(incoming, "\"type\":\"route_resp\"") != nullptr)
  {
    char status[16] = {0};
    extractJsonString(incoming, "status", status, sizeof(status));

    if (strcmp(status, "ok") == 0)
    {
      char nextHop[32] = {0};
      extractJsonString(incoming, "next_hop", nextHop, sizeof(nextHop));

      if (nextHop[0] != '\0')
      {
        Serial.print(F("Route hint next_hop="));
        Serial.println(nextHop);
      }
    }
  }
  else
  {
    Serial.println(F("[CTRL] No known control message type matched"));
  }
}

// ============== CONTROL FRAME POLLING ==============
void pollControlFrames(unsigned long maxMs)
{
  unsigned long start = millis();
  while ((millis() - start) < maxMs)
  {
    if (!rf95.available())
    {
      break;
    }

    uint8_t recvBuf[RH_RF95_MAX_MESSAGE_LEN];
    uint8_t recvLen = sizeof(recvBuf);
    if (!rf95.recv(recvBuf, &recvLen))
    {
      if (!joined)
      {
        Serial.println(F("[RX CTRL] available but recv failed"));
      }
      break;
    }

    if (!joined)
    {
      Serial.print(F("[RX CTRL] raw frame len="));
      Serial.print(recvLen);
      Serial.print(F(" rssi="));
      Serial.println(rf95.lastRssi());
    }

    Packet maybeAck;
    if (deserializePacket(recvBuf, recvLen, &maybeAck))
    {
      if (!joined)
      {
        Serial.print(F("[RX CTRL] ignored binary frame while waiting join, len="));
        Serial.print(recvLen);
        Serial.print(F(" type="));
        Serial.println((int)maybeAck.type);
      }
      continue;
    }

    char raw[RH_RF95_MAX_MESSAGE_LEN + 1];
    uint8_t copyLen = recvLen;
    if (copyLen > RH_RF95_MAX_MESSAGE_LEN)
    {
      copyLen = RH_RF95_MAX_MESSAGE_LEN;
    }

    Serial.print(F("[RX CTR HEX]"));
    for (uint8_t i = 0; i < recvLen; i++)
    {
      if (recvBuf[i] < 0x10) Serial.print('0');
      Serial.print(recvBuf[i], HEX);
      Serial.print(' ');
    }
    Serial.println();

    recvBuf[recvLen] = '\0';           // make it a C string
    Serial.print(F("[RX CTRL] "));
    Serial.println((char*)recvBuf);
    handleControlMessage((char*)recvBuf);

    


  }
}

// ============== OLED ==============
void oledPrint(const char *msg)
{
#if ENABLE_OLED
  if (!oledOK)
    return;
  display.clearDisplay();
  display.setCursor(0, 0);
  display.println(msg);
  display.display();
#else
  (void)msg;
#endif
}

void oledStatus(float temp, float hum, const char *status)
{
#if ENABLE_OLED
  if (!oledOK)
    return;
  display.clearDisplay();
  display.setCursor(0, 0);
  display.print(F("T:"));
  display.print(temp, 1);
  display.print(F("C H:"));
  display.print(hum, 1);
  display.println(F("%"));
  display.println(status);
  display.display();
#else
  (void)temp;
  (void)hum;
  (void)status;
#endif
}

// ============== CHECKSUM ==============
uint8_t calculateChecksum(Packet *pkt)
{
  uint8_t cs = 0;
  cs ^= pkt->start;
  cs ^= pkt->from;
  cs ^= pkt->to;
  cs ^= pkt->type;
  cs ^= pkt->seq;
  cs ^= pkt->len;
  for (uint8_t i = 0; i < pkt->len; i++)
  {
    cs ^= pkt->payload[i];
  }
  return cs;
}

// ============== PACKET FUNCTIONS ==============
void createDataPacket(Packet *pkt, uint8_t targetId, const char *message)
{
  pkt->start = PACKET_START;
  pkt->from = MY_NODE_ID;
  pkt->to = targetId;
  pkt->type = MSG_TYPE_DATA;
  pkt->seq = sequenceNum;
  pkt->len = strlen(message);
  if (pkt->len > MAX_PAYLOAD)
    pkt->len = MAX_PAYLOAD;
  memcpy(pkt->payload, message, pkt->len);
  pkt->checksum = calculateChecksum(pkt);
}

uint8_t serializePacket(Packet *pkt, uint8_t *buffer)
{
  uint8_t idx = 0;
  buffer[idx++] = pkt->start;
  buffer[idx++] = pkt->from;
  buffer[idx++] = pkt->to;
  buffer[idx++] = pkt->type;
  buffer[idx++] = pkt->seq;
  buffer[idx++] = pkt->len;
  for (uint8_t i = 0; i < pkt->len; i++)
  {
    buffer[idx++] = pkt->payload[i];
  }
  buffer[idx++] = pkt->checksum;
  return idx;
}

bool deserializePacket(uint8_t *buffer, uint8_t bufLen, Packet *pkt)
{
  if (bufLen < HEADER_SIZE + 1)
    return false;
  if (buffer[0] != PACKET_START)
    return false;

  pkt->start = buffer[0];
  pkt->from = buffer[1];
  pkt->to = buffer[2];
  pkt->type = buffer[3];
  pkt->seq = buffer[4];
  pkt->len = buffer[5];

  if (pkt->len > MAX_PAYLOAD)
    return false;
  if (bufLen < HEADER_SIZE + pkt->len + 1)
    return false;

  for (uint8_t i = 0; i < pkt->len; i++)
  {
    pkt->payload[i] = buffer[HEADER_SIZE + i];
  }
  pkt->checksum = buffer[HEADER_SIZE + pkt->len];

  return (calculateChecksum(pkt) == pkt->checksum);
}

void printHexBytes(const uint8_t *data, uint8_t len)
{
  for (uint8_t i = 0; i < len; i++)
  {
    if (data[i] < 0x10)
      Serial.print('0');
    Serial.print(data[i], HEX);
    if (i + 1 < len)
      Serial.print(' ');
  }
}

bool isAckForTx(const Packet *ackPkt, const Packet *txPkt, uint8_t expectedFrom)
{
  return (ackPkt->to == MY_NODE_ID &&
          ackPkt->from == expectedFrom &&
          ackPkt->type == MSG_TYPE_ACK &&
          ackPkt->seq == txPkt->seq);
}

void flushRxQueue(unsigned long maxMs)
{
  unsigned long start = millis();
  uint8_t junk[50];
  uint8_t junkLen = sizeof(junk);

  while ((millis() - start) < maxMs)
  {
    if (!rf95.available())
      break;
    junkLen = sizeof(junk);
    if (!rf95.recv(junk, &junkLen))
      break;

    if (DEBUG_ACK)
    {
      Serial.print(F("Flushed stale RX frame len="));
      Serial.println(junkLen);
    }
    delay(2);
  }
}

// ============== SEND WITH RETRY ==============
bool sendWithRetry(uint8_t targetId, const char *message)
{
  Packet txPacket;
  createDataPacket(&txPacket, targetId, message);

  uint8_t buffer[40];
  uint8_t len = serializePacket(&txPacket, buffer);

  Serial.print(F("Sending: "));
  Serial.println(message);

  for (uint8_t attempt = 1; attempt <= MAX_RETRIES; attempt++)
  {
    Serial.print(F("Attempt "));
    Serial.print(attempt);
    Serial.print(F("/"));
    Serial.println(MAX_RETRIES);

    // Clear any stale frame from previous attempts before sending this packet.
    flushRxQueue(30);

    rf95.send(buffer, len);
    rf95.waitPacketSent();

    // Give the radio a brief guard time to re-enter RX before ACK is sent back.
    rf95.setModeRx();
    delay(ACK_RX_GUARD_MS);

    if (!REQUIRE_ACK)
    {
      Serial.println(F("ACK disabled: treating as delivered"));
      sequenceNum++;
      return true;
    }

    bool ackMatched = false;
    unsigned long ackStart = millis();
    while ((millis() - ackStart) < ACK_TIMEOUT)
    {
      uint16_t remaining = ACK_TIMEOUT - (uint16_t)(millis() - ackStart);
      if (rf95.waitAvailableTimeout(remaining))
      {
        uint8_t recvBuf[50];
        uint8_t recvLen = sizeof(recvBuf);
        if (!rf95.recv(recvBuf, &recvLen))
        {
          continue;
        }

        if (DEBUG_ACK)
        {
          Serial.print(F("RX candidate len="));
          Serial.print(recvLen);
          Serial.print(F(" bytes: "));
          printHexBytes(recvBuf, recvLen);
          Serial.println();
        }

        Packet ackPkt;
        if (deserializePacket(recvBuf, recvLen, &ackPkt))
        {
          if (DEBUG_ACK)
          {
            Serial.print(F("ACK parsed: from="));
            Serial.print((char)ackPkt.from);
            Serial.print(F(" to="));
            Serial.print((char)ackPkt.to);
            Serial.print(F(" seq="));
            Serial.println(ackPkt.seq);
          }

          if (isAckForTx(&ackPkt, &txPacket, targetId))
          {
            ackMatched = true;
            break;
          }

          if (DEBUG_ACK)
          {
            Serial.println(F("ACK parsed but does not match this TX (from/to/type/seq mismatch)"));
          }
        }
        else if (DEBUG_ACK)
        {
          char raw[RH_RF95_MAX_MESSAGE_LEN + 1];
          uint8_t copyLen = recvLen;
          if (copyLen > RH_RF95_MAX_MESSAGE_LEN)
          {
            copyLen = RH_RF95_MAX_MESSAGE_LEN;
          }
          memcpy(raw, recvBuf, copyLen);
          raw[copyLen] = '\0';

          Serial.print(F("RX non-ACK frame: "));
          Serial.println(raw);
          handleControlMessage(raw);
        }
      }
      else
      {
        break;
      }
    }

    if (ackMatched)
    {
      Serial.println(F("ACK OK!"));
      sequenceNum++;
      return true;
    }

    if (DEBUG_ACK)
    {
      Serial.println(F("ACK wait timeout: no matching ACK"));
    }
    Serial.println(F("No ACK, retrying..."));
    delay(200 + random(200));
  }

  Serial.println(F("Delivery failed"));
  sequenceNum++;
  return false;
}

// ============== SETUP ==============
void setup()
{
  Serial.begin(9600);
  delay(100);
  Serial.println(F("=== LoRa TX + DHT22 ==="));

  Wire.begin();
  dht.begin();

  // OLED init
#if ENABLE_OLED
  if (display.begin(SSD1306_SWITCHCAPVCC, 0x3C))
  {
    oledOK = true;
  }
  else if (display.begin(SSD1306_SWITCHCAPVCC, 0x3D))
  {
    oledOK = true;
  }
  if (oledOK)
  {
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.clearDisplay();
    display.display();
    oledPrint("Init...");
  }
#endif

  // LoRa init
  pinMode(RFM95_RST, OUTPUT);
  digitalWrite(RFM95_RST, HIGH);
  delay(10);
  digitalWrite(RFM95_RST, LOW);
  delay(10);
  digitalWrite(RFM95_RST, HIGH);
  delay(10);

  if (!rf95.init())
  {
    Serial.println(F("LoRa FAILED"));
    while (1)
      ;
  }
  if (!rf95.setFrequency(RF95_FREQ))
  {
    Serial.println(F("Freq FAILED"));
    while (1)
      ;
  }
  rf95.setTxPower(13, false);

  Serial.println(F("Setup OK!"));
  oledPrint("Ready");
  delay(1000);

  sendJoinRequest();
  lastJoinTime = millis();
}

// ============== LOOP ==============
void loop()
{
  unsigned long now = millis();

  pollControlFrames(20);

  if (!joined && (now - lastJoinTime >= JOIN_INTERVAL))
  {
    sendJoinRequest();
    lastJoinTime = now;
    oledPrint("Joining mesh...");
  }

  if (joined && (now - lastHelloTime >= HELLO_INTERVAL))
  {
    sendHelloPacket();
    lastHelloTime = now;
  }

  if (joined && (now - lastRouteQueryTime >= ROUTE_QUERY_INTERVAL))
  {
    sendRouteQuery("dashboard");
    lastRouteQueryTime = now;
  }

  if (!joined)
  {
    delay(200);
    return;
  }

  // Read DHT22
  float humidity = dht.readHumidity();
  float temperature = dht.readTemperature(); // Celsius

  // Check if read failed
  if (isnan(humidity) || isnan(temperature))
  {
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
  dtostrf(temperature, 4, 1, message); // e.g. "24.5"
  char humStr[8];
  dtostrf(humidity, 4, 1, humStr); // e.g. "61.2"

  // Build "T:24.5,H:61.2"
  char payload[MAX_PAYLOAD];
  snprintf(payload, MAX_PAYLOAD, "T:%s,H:%s", message, humStr);

  Serial.print(F("Payload: "));
  Serial.println(payload);

  oledStatus(temperature, humidity, "Sending...");

  bool ok = sendWithRetry(TARGET_NODE_ID, payload);

  if (ok)
  {
    Serial.println(F("Delivered!"));
    oledStatus(temperature, humidity, "Delivered!");
  }
  else
  {
    oledStatus(temperature, humidity, "Failed!");
  }

  // DHT22 needs minimum 2s between reads
  delay(5000);
}