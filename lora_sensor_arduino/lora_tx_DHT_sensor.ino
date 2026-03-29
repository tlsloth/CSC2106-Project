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
#define MY_NODE_ID 'dht_sensor_A'
#define TARGET_NODE_ID 'bridge_01'

// Mesh join/discovery control channel (JSON over LoRa)
const char *MESH_NODE_ID = "dht_sensor_A";
const char *MESH_NETWORK_NAME = "CSC2106_MESH";
const char *MESH_JOIN_KEY = "mesh_key_v1";

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

// ===== Original String-based helpers (still available if needed) =====
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

void sendTelemetryJson(float temperature, float humidity)
{
  // First convert floats to strings safely
  char tempStr[16];
  char humStr[16];

  // Ensure we have valid numbers before conversion
  if (isnan(temperature) || isnan(humidity))
  {
    Serial.println(F("[TX DATA] NaN values, skipping telemetry"));
    return;
  }

  dtostrf(temperature, 0, 1, tempStr); // e.g. "26.2"
  dtostrf(humidity,   0, 1, humStr);   // e.g. "58.0"

  char payload[200];
  uint8_t seq = sequenceNum++;

  // Now build JSON using the stringified numbers
  int written = snprintf(
      payload,
      sizeof(payload),
      "{\"type\":\"sensor_data\",\"node_id\":\"%s\",\"temp\":%s,\"hum\":%s,\"seq\":%u}",
      MESH_NODE_ID,
      tempStr,
      humStr,
      (unsigned int)seq
  );

  Serial.print(F("[TX DATA] "));
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

    // Print hex
    Serial.print(F("[RX CTR HEX]"));
    for (uint8_t i = 0; i < recvLen; i++)
    {
      if (recvBuf[i] < 0x10) Serial.print('0');
      Serial.print(recvBuf[i], HEX);
      Serial.print(' ');
    }
    Serial.println();

    // Treat as JSON text
    if (recvLen >= RH_RF95_MAX_MESSAGE_LEN) {
      recvLen = RH_RF95_MAX_MESSAGE_LEN - 1;
    }
    recvBuf[recvLen] = '\0';

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

// ============== SETUP ==============
void setup()
{
  Serial.begin(9600);
  delay(100);
  Serial.println(F("=== LoRa TX + DHT22 (JSON-only) ==="));

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

  oledStatus(temperature, humidity, "Sending...");

  // JSON telemetry
  sendTelemetryJson(temperature, humidity);

  // DHT22 needs minimum 2s between reads
  delay(5000);
}