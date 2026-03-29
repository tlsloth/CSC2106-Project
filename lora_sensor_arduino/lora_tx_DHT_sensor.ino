#include <SPI.h>
#include <RH_RF95.h>
#include <DHT.h>

#define RFM95_CS 10
#define RFM95_RST 9
#define RFM95_INT 2
#define RF95_FREQ 920.0

#define DHTPIN 3
#define DHTTYPE DHT22

const char MESH_NODE_ID[] = "dht_sensor_A";
const char MESH_NETWORK_NAME[] = "CSC2106_MESH";
const char MESH_JOIN_KEY[] = "mesh_key_v1";
const char TARGET_ROUTE_DST[] = "dashboard";

RH_RF95 rf95(RFM95_CS, RFM95_INT);
DHT dht(DHTPIN, DHTTYPE);

uint8_t sequenceNum = 0;
bool joined = false;
bool awaitingRouteResponse = false;

char learned_bridge_id[20] = "bridge_01";
char meshToken[48] = "";

unsigned long lastJoinTime = 0;
unsigned long lastHelloTime = 0;
unsigned long lastRouteQueryTime = 0;
unsigned long routeQueryDeadline = 0;

const unsigned long JOIN_INTERVAL = 10000UL;
const unsigned long HELLO_INTERVAL = 5000UL;
const unsigned long ROUTE_QUERY_INTERVAL = 15000UL;
const unsigned long ROUTE_QUERY_TIMEOUT = 10000UL;

static bool streq(const char *a, const char *b)
{
  return strcmp(a, b) == 0;
}

static bool contains(const char *haystack, const char *needle)
{
  return strstr(haystack, needle) != nullptr;
}

static int findJsonValueStart(const char *json, const char *key)
{
  if (!json || !key)
    return -1;

  char needle[24];
  snprintf(needle, sizeof(needle), "\"%s\"", key);

  const char *keyPos = strstr(json, needle);
  if (!keyPos)
    return -1;

  const char *colon = strchr(keyPos + strlen(needle), ':');
  if (!colon)
    return -1;

  const char *p = colon + 1;
  while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n')
    p++;

  return (int)(p - json);
}

static bool extractJsonBool(const char *json, const char *key)
{
  int start = findJsonValueStart(json, key);
  if (start < 0)
    return false;

  const char *p = json + start;
  return (strncmp(p, "true", 4) == 0) || (*p == '1');
}

static bool extractJsonString(const char *json, const char *key, char *out, size_t outSize)
{
  if (!json || !key || !out || outSize == 0)
    return false;

  out[0] = '\0';

  char needle[24];
  snprintf(needle, sizeof(needle), "\"%s\"", key);

  const char *keyPos = strstr(json, needle);
  if (!keyPos)
    return false;

  const char *colon = strchr(keyPos + strlen(needle), ':');
  if (!colon)
    return false;

  const char *p = colon + 1;
  while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n')
    p++;

  if (*p != '"')
    return false;
  p++;

  const char *end = strchr(p, '"');
  if (!end)
    return false;

  size_t len = (size_t)(end - p);
  if (len >= outSize)
    len = outSize - 1;

  memcpy(out, p, len);
  out[len] = '\0';
  return true;
}

static bool isLikelyJsonText(const uint8_t *buf, uint8_t len)
{
  if (!buf || len < 2)
    return false;

  uint8_t start = 0;
  while (start < len && (buf[start] == ' ' || buf[start] == '\t' || buf[start] == '\r' || buf[start] == '\n'))
    start++;
  if (start >= len || buf[start] != '{')
    return false;

  int end = len - 1;
  while (end >= 0 && (buf[end] == ' ' || buf[end] == '\t' || buf[end] == '\r' || buf[end] == '\n'))
    end--;
  if (end < 0 || buf[end] != '}')
    return false;

  return true;
}

static uint32_t fnv1a32(const char *str)
{
  uint32_t hash = 2166136261UL;
  while (*str)
  {
    hash ^= (uint8_t)*str++;
    hash *= 16777619UL;
  }
  return hash;
}

// XOR-decrypt a hex-encoded, XOR-encrypted token string using the mesh join key.
// encoded: encrypted hex string  key: MESH_JOIN_KEY  out: buffer to write decrypted hex into
static bool decryptToken(const char *encoded, const char *key, char *out, size_t outSize)
{
  size_t hexLen = strlen(encoded);
  if (hexLen == 0 || hexLen % 2 != 0)
    return false;
  size_t byteCount = hexLen / 2;
  if (byteCount * 2 + 1 > outSize)
    return false;
  size_t keyLen = strlen(key);
  if (keyLen == 0)
    return false;
  for (size_t i = 0; i < byteCount; i++)
  {
    char hi = encoded[i * 2], lo = encoded[i * 2 + 1];
    uint8_t h = (hi >= '0' && hi <= '9') ? hi - '0' : (hi >= 'a' && hi <= 'f') ? hi - 'a' + 10
                                                                               : hi - 'A' + 10;
    uint8_t l = (lo >= '0' && lo <= '9') ? lo - '0' : (lo >= 'a' && lo <= 'f') ? lo - 'a' + 10
                                                                               : lo - 'A' + 10;
    uint8_t b = ((h << 4) | l) ^ (uint8_t)key[i % keyLen];
    snprintf(out + i * 2, 3, "%02x", b);
  }
  return true;
}

void sendRawLoRa(const char *payload)
{
  if (!payload || !payload[0])
    return;
  rf95.send((const uint8_t *)payload, strlen(payload));
  rf95.waitPacketSent();
  rf95.setModeRx();
}

void sendJoinRequest()
{
  char payload[180];
  uint8_t seq = sequenceNum++;

  char authHex[9];
  snprintf(authHex, sizeof(authHex), "%08lx", (unsigned long)fnv1a32(MESH_JOIN_KEY));

  int written;
  if (meshToken[0] != '\0')
  {
    written = snprintf(
        payload, sizeof(payload),
        "{\"type\":\"join_req\",\"node_id\":\"%s\",\"network\":\"%s\",\"auth\":\"%s\",\"token\":\"%s\",\"seq\":%u}",
        MESH_NODE_ID, MESH_NETWORK_NAME, authHex, meshToken, (unsigned int)seq);
  }
  else
  {
    written = snprintf(
        payload, sizeof(payload),
        "{\"type\":\"join_req\",\"node_id\":\"%s\",\"network\":\"%s\",\"auth\":\"%s\",\"seq\":%u}",
        MESH_NODE_ID, MESH_NETWORK_NAME, authHex, (unsigned int)seq);
  }

  if (written > 0 && written < (int)sizeof(payload))
  {
    sendRawLoRa(payload);
  }
}

void sendHelloPacket()
{
  char payload[180];
  uint8_t seq = sequenceNum++;

  char encToken[48];
  encToken[0] = '\0';
  if (meshToken[0] != '\0')
    decryptToken(meshToken, MESH_JOIN_KEY, encToken, sizeof(encToken));

  int written = snprintf(
      payload, sizeof(payload),
      "{\"type\":\"hello\",\"node_id\":\"%s\",\"network\":\"%s\",\"token\":\"%s\",\"seq\":%u}",
      MESH_NODE_ID, MESH_NETWORK_NAME, encToken, (unsigned int)seq);

  if (written > 0 && written < (int)sizeof(payload))
  {
    sendRawLoRa(payload);
  }
}

void sendRouteQuery(const char *dst)
{
  char payload[180];
  uint8_t seq = sequenceNum++;

  char encToken[48];
  encToken[0] = '\0';
  if (meshToken[0] != '\0')
    decryptToken(meshToken, MESH_JOIN_KEY, encToken, sizeof(encToken));

  int written = snprintf(
      payload, sizeof(payload),
      "{\"type\":\"route_query\",\"src\":\"%s\",\"dst\":\"%s\",\"token\":\"%s\",\"seq\":%u}",
      MESH_NODE_ID, dst, encToken, (unsigned int)seq);

  if (written > 0 && written < (int)sizeof(payload))
  {
    sendRawLoRa(payload);
    awaitingRouteResponse = true;
    routeQueryDeadline = millis() + ROUTE_QUERY_TIMEOUT;
  }
}

void sendTelemetryJson(float temperature, float humidity)
{
  if (isnan(temperature) || isnan(humidity))
    return;

  char tempStr[12];
  char humStr[12];
  char payload[180];

  dtostrf(temperature, 0, 1, tempStr);
  dtostrf(humidity, 0, 1, humStr);

  uint8_t seq = sequenceNum++;
  char encToken[48];
  encToken[0] = '\0';
  if (meshToken[0] != '\0')
    decryptToken(meshToken, MESH_JOIN_KEY, encToken, sizeof(encToken));

  int written = snprintf(
      payload, sizeof(payload),
      "{\"type\":\"sensor_data\",\"node_id\":\"%s\",\"temp\":%s,\"hum\":%s,\"token\":\"%s\",\"seq\":%u}",
      MESH_NODE_ID, tempStr, humStr, encToken, (unsigned int)seq);

  if (written > 0 && written < (int)sizeof(payload))
  {
    sendRawLoRa(payload);
  }
}

void handleControlMessage(const char *incoming)
{
  if (!incoming || !incoming[0])
    return;

  if (contains(incoming, "\"type\":\"join_ack\""))
  {
    char targetId[20];
    targetId[0] = '\0';
    extractJsonString(incoming, "target_id", targetId, sizeof(targetId));
    if (!streq(targetId, MESH_NODE_ID))
    {
      return;
    }

    if (extractJsonBool(incoming, "accepted"))
    {
      char bridgeId[20];
      char encToken[48];

      bridgeId[0] = '\0';
      encToken[0] = '\0';

      extractJsonString(incoming, "bridge_id", bridgeId, sizeof(bridgeId));
      extractJsonString(incoming, "token", encToken, sizeof(encToken));

      if (bridgeId[0] != '\0')
      {
        strncpy(learned_bridge_id, bridgeId, sizeof(learned_bridge_id) - 1);
        learned_bridge_id[sizeof(learned_bridge_id) - 1] = '\0';
      }

      if (encToken[0] == '\0' || !decryptToken(encToken, MESH_JOIN_KEY, meshToken, sizeof(meshToken)))
      {
        joined = false;
        meshToken[0] = '\0';
        awaitingRouteResponse = false;
        routeQueryDeadline = 0;
        lastJoinTime = 0;
        return;
      }

      joined = true;
      awaitingRouteResponse = false;
      routeQueryDeadline = 0;
      lastRouteQueryTime = 0;
    }
    else
    {
      joined = false;
      meshToken[0] = '\0';
      awaitingRouteResponse = false;
      routeQueryDeadline = 0;
    }
  }
  else if (contains(incoming, "\"type\":\"route_resp\""))
  {
    awaitingRouteResponse = false;
    routeQueryDeadline = 0;

    char status[12];
    status[0] = '\0';

    extractJsonString(incoming, "status", status, sizeof(status));

    if (streq(status, "ok"))
    {
      char nextHop[20];
      nextHop[0] = '\0';
      extractJsonString(incoming, "next_hop", nextHop, sizeof(nextHop));
    }
  }
}

void pollControlFrames(unsigned long maxMs)
{
  unsigned long start = millis();

  while ((millis() - start) < maxMs)
  {
    if (!rf95.available())
      break;

    uint8_t recvBuf[RH_RF95_MAX_MESSAGE_LEN + 1];
    uint8_t recvLen = RH_RF95_MAX_MESSAGE_LEN;

    if (!rf95.recv(recvBuf, &recvLen))
    {
      break;
    }

    if (!isLikelyJsonText(recvBuf, recvLen))
    {
      continue;
    }

    recvBuf[recvLen] = '\0';
    handleControlMessage((const char *)recvBuf);
  }
}

void setup()
{
  Serial.begin(9600);
  dht.begin();

  pinMode(RFM95_RST, OUTPUT);
  digitalWrite(RFM95_RST, HIGH);
  delay(10);
  digitalWrite(RFM95_RST, LOW);
  delay(10);
  digitalWrite(RFM95_RST, HIGH);
  delay(10);

  if (!rf95.init())
  {
    while (1)
    {
    }
  }

  if (!rf95.setFrequency(RF95_FREQ))
  {
    while (1)
    {
    }
  }

  rf95.setTxPower(13, false);
  sendJoinRequest();
  lastJoinTime = millis();
}

void loop()
{
  unsigned long now = millis();

  pollControlFrames(20);

  if (!joined && (now - lastJoinTime >= JOIN_INTERVAL))
  {
    sendJoinRequest();
    lastJoinTime = now;
  }

  if (joined && (now - lastHelloTime >= HELLO_INTERVAL))
  {
    sendHelloPacket();
    lastHelloTime = now;
  }

  if (joined && (now - lastRouteQueryTime >= ROUTE_QUERY_INTERVAL))
  {
    sendRouteQuery(TARGET_ROUTE_DST);
    lastRouteQueryTime = now;
  }

  if (joined && awaitingRouteResponse && (long)(now - routeQueryDeadline) >= 0)
  {
    joined = false;
    meshToken[0] = '\0';
    awaitingRouteResponse = false;
    routeQueryDeadline = 0;
    lastJoinTime = 0;
  }

  if (!joined)
  {
    delay(200);
    return;
  }

  float humidity = dht.readHumidity();
  float temperature = dht.readTemperature();

  if (isnan(humidity) || isnan(temperature))
  {
    delay(2000);
    return;
  }

  sendTelemetryJson(temperature, humidity);
  delay(5000);
}