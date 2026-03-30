#include "BridgeMesh.h"
#include <string.h>
#include <stdio.h>

static const unsigned long HELLO_RX_GUARD_MS = 350UL;
static const unsigned long HELLO_JITTER_MS = 250UL;
static const unsigned long HELLO_RETRY_AFTER_MISS_MS = 1200UL;
static const uint8_t HELLO_ACK_MISS_THRESHOLD = 3;

BridgeMesh::BridgeMesh(RH_RF95 &radio, const BridgeMeshConfig &config)
    : _radio(radio),
      _config(config),
      _joined(false),
      _awaitingRouteResponse(false),
      _missedHelloAcks(0),
      _seq(0),
      _lastJoinTime(0),
      _lastHelloTime(0),
      _lastRouteQueryTime(0),
      _routeQueryDeadline(0),
      _txHoldUntil(0)
{
  _token[0] = '\0';
  strncpy(_bridgeId, "bridge_01", sizeof(_bridgeId) - 1);
  _bridgeId[sizeof(_bridgeId) - 1] = '\0';
}

bool BridgeMesh::begin()
{
  _joined = false;
  _awaitingRouteResponse = false;
  _missedHelloAcks = 0;
  _seq = 0;
  _lastJoinTime = 0;
  _lastHelloTime = 0;
  _lastRouteQueryTime = 0;
  _routeQueryDeadline = 0;
  _txHoldUntil = 0;
  _token[0] = '\0';
  randomSeed(micros());
  return sendJoinRequest();
}

const BridgeMeshConfig &BridgeMesh::config() const
{
  return _config;
}

bool BridgeMesh::isJoined() const
{
  return _joined;
}

const char *BridgeMesh::token() const
{
  return _token;
}

const char *BridgeMesh::bridgeId() const
{
  return _bridgeId;
}

bool BridgeMesh::sendRaw(const char *payload)
{
  if (!payload || !payload[0])
    return false;

  _radio.send((const uint8_t *)payload, strlen(payload));
  _radio.waitPacketSent();
  _radio.setModeRx();
  return true;
}

uint32_t BridgeMesh::fnv1a32(const char *str)
{
  uint32_t hash = 2166136261UL;
  while (*str)
  {
    hash ^= (uint8_t)*str++;
    hash *= 16777619UL;
  }
  return hash;
}

// XOR-decrypt a hex-encoded, XOR-encrypted token using join key.
// This is exactly your previous decryptToken logic.
bool BridgeMesh::decryptToken(const char *encoded, const char *key, char *out, size_t outSize)
{
  size_t hexLen = strlen(encoded);
  if (hexLen == 0 || (hexLen % 2) != 0)
    return false;

  size_t byteCount = hexLen / 2;
  if (byteCount * 2 + 1 > outSize)
    return false;

  size_t keyLen = strlen(key);
  if (keyLen == 0)
    return false;

  for (size_t i = 0; i < byteCount; i++)
  {
    char hi = encoded[i * 2];
    char lo = encoded[i * 2 + 1];

    uint8_t h = (hi >= '0' && hi <= '9')   ? hi - '0'
                : (hi >= 'a' && hi <= 'f') ? hi - 'a' + 10
                                           : hi - 'A' + 10;
    uint8_t l = (lo >= '0' && lo <= '9')   ? lo - '0'
                : (lo >= 'a' && lo <= 'f') ? lo - 'a' + 10
                                           : lo - 'A' + 10;

    uint8_t b = ((h << 4) | l) ^ (uint8_t)key[i % keyLen];
    snprintf(out + i * 2, 3, "%02x", b);
  }
  return true;
}

bool BridgeMesh::sendJoinRequest()
{
  char payload[180];

  uint8_t seq = _seq++;

  char authHex[9];
  snprintf(authHex, sizeof(authHex), "%08lx", (unsigned long)fnv1a32(_config.joinKey));

  int written;
  if (_token[0] != '\0')
  {
    written = snprintf(
        payload,
        sizeof(payload),
        "{\"type\":\"join_req\",\"node_id\":\"%s\",\"network\":\"%s\",\"auth\":\"%s\",\"token\":\"%s\",\"seq\":%u}",
        _config.nodeId,
        _config.networkName,
        authHex,
        _token,
        (unsigned)seq);
  }
  else
  {
    written = snprintf(
        payload,
        sizeof(payload),
        "{\"type\":\"join_req\",\"node_id\":\"%s\",\"network\":\"%s\",\"auth\":\"%s\",\"seq\":%u}",
        _config.nodeId,
        _config.networkName,
        authHex,
        (unsigned)seq);
  }

  if (written <= 0 || written >= (int)sizeof(payload))
  {
    return false;
  }

  bool ok = sendRaw(payload);
  if (ok)
  {
    _lastJoinTime = millis();
  }
  return ok;
}

bool BridgeMesh::sendHello()
{
  if (!_joined)
    return false;

  char payload[180];
  uint8_t seq = _seq++;

  char encToken[48];
  encToken[0] = '\0';

  if (_token[0] != '\0')
  {
    decryptToken(_token, _config.joinKey, encToken, sizeof(encToken));
  }

  int written = snprintf(
      payload,
      sizeof(payload),
      "{\"type\":\"hello\",\"node_id\":\"%s\",\"network\":\"%s\",\"token\":\"%s\",\"seq\":%u}",
      _config.nodeId,
      _config.networkName,
      encToken,
      (unsigned)seq);

  if (written <= 0 || written >= (int)sizeof(payload))
  {
    return false;
  }

  bool ok = sendRaw(payload);
  if (ok)
  {
    unsigned long now = millis();
    _lastHelloTime = now;
    // Liveness now relies on hello acknowledgements from the bridge.
    _awaitingRouteResponse = true;
    _routeQueryDeadline = now + _config.routeQueryTimeout;
    // Stay in RX mode for a short guard window so hello_ack can be received.
    _txHoldUntil = now + HELLO_RX_GUARD_MS;
  }
  return ok;
}

bool BridgeMesh::sendRouteQuery(const char *dst)
{
  if (!_joined || !dst || !dst[0])
  {
    return false;
  }

  if ((long)(millis() - _txHoldUntil) < 0)
  {
    return false;
  }

  char payload[180];
  uint8_t seq = _seq++;

  char encToken[48];
  encToken[0] = '\0';

  if (_token[0] != '\0')
  {
    decryptToken(_token, _config.joinKey, encToken, sizeof(encToken));
  }

  int written = snprintf(
      payload,
      sizeof(payload),
      "{\"type\":\"route_query\",\"src\":\"%s\",\"dst\":\"%s\",\"token\":\"%s\",\"seq\":%u}",
      _config.nodeId,
      dst,
      encToken,
      (unsigned)seq);

  if (written <= 0 || written >= (int)sizeof(payload))
  {
    return false;
  }

  if (!sendRaw(payload))
  {
    return false;
  }

  // Route checks are best-effort and do not affect join/liveness state.
  _lastRouteQueryTime = millis();
  return true;
}

bool BridgeMesh::sendJsonObject(const char *jsonObject, const char *type)
{
  if (!_joined || !jsonObject || jsonObject[0] != '{')
  {
    return false;
  }

  if ((long)(millis() - _txHoldUntil) < 0)
  {
    return false;
  }

  char encToken[48];
  encToken[0] = '\0';

  if (_token[0] != '\0')
  {
    decryptToken(_token, _config.joinKey, encToken, sizeof(encToken));
  }

  // Wrap application payload into mesh envelope.
  // Mesh token is sent encrypted as before.
  char packet[240];

  int written = snprintf(
      packet,
      sizeof(packet),
      "{\"type\":\"%s\",\"node_id\":\"%s\",\"token\":\"%s\",\"payload\":%s}",
      type,
      _config.nodeId,
      encToken,
      jsonObject);

  if (written <= 0 || written >= (int)sizeof(packet))
  {
    return false;
  }

  return sendRaw(packet);
}

void BridgeMesh::tick()
{
  unsigned long now = millis();

  if (!_joined)
  {
    if (_lastJoinTime == 0 || (now - _lastJoinTime >= _config.joinInterval))
    {
      sendJoinRequest();
    }
    return;
  }

  if (!_awaitingRouteResponse && (_lastHelloTime == 0 || (now - _lastHelloTime >= _config.helloInterval)))
  {
    sendHello();
    if (_lastHelloTime != 0)
    {
      unsigned long jitter = random(0, HELLO_JITTER_MS + 1);
      _lastHelloTime += jitter;
    }
  }

  if (_config.routeDst && _config.routeDst[0] != '\0')
  {
    if (_lastRouteQueryTime == 0 || (now - _lastRouteQueryTime >= _config.routeQueryInterval))
    {
      sendRouteQuery(_config.routeDst);
    }
  }

  if (_awaitingRouteResponse && (long)(now - _routeQueryDeadline) >= 0)
  {
    _awaitingRouteResponse = false;
    _routeQueryDeadline = 0;
    _txHoldUntil = 0;
    _missedHelloAcks++;

    if (_missedHelloAcks >= HELLO_ACK_MISS_THRESHOLD)
    {
      _joined = false;
      _token[0] = '\0';
      _lastJoinTime = 0;
      _missedHelloAcks = 0;
    }
    else
    {
      // Retry hello after a short delay, but do not spam every loop.
      if (_config.helloInterval > HELLO_RETRY_AFTER_MISS_MS)
      {
        _lastHelloTime = now - (_config.helloInterval - HELLO_RETRY_AFTER_MISS_MS);
      }
      else
      {
        _lastHelloTime = now;
      }
      _txHoldUntil = now + HELLO_RETRY_AFTER_MISS_MS;
    }
  }
}

void BridgeMesh::poll(unsigned long maxMs)
{
  unsigned long start = millis();

  while ((millis() - start) < maxMs)
  {
    if (!_radio.available())
      break;

    uint8_t recvBuf[RH_RF95_MAX_MESSAGE_LEN + 1];
    uint8_t recvLen = RH_RF95_MAX_MESSAGE_LEN;

    if (!_radio.recv(recvBuf, &recvLen))
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

void BridgeMesh::handleControlMessage(const char *incoming)
{
  if (!incoming || !incoming) return;

  // Only care about explicit control types
  if (!contains(incoming, "\"type\":\"join_ack\"") &&
      !contains(incoming, "\"type\":\"hello_ack\"") &&
      !contains(incoming, "\"type\":\"route_resp\"")) {
    return;  // ignore sensor_data and random JSON
  }

  Serial.println(incoming);
  if (contains(incoming, "\"type\":\"join_ack\""))
  {
    char targetId[32];
    targetId[0] = '\0';
    extractJsonString(incoming, "target_id", targetId, sizeof(targetId));
    if (targetId[0] && strcmp(targetId, _config.nodeId) != 0)
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
        strncpy(_bridgeId, bridgeId, sizeof(_bridgeId) - 1);
        _bridgeId[sizeof(_bridgeId) - 1] = '\0';
      }

      if (encToken[0] == '\0' || !decryptToken(encToken, _config.joinKey, _token, sizeof(_token)))
      {
        _joined = false;
        _token[0] = '\0';
        _awaitingRouteResponse = false;
        _routeQueryDeadline = 0;
        _txHoldUntil = 0;
        _missedHelloAcks = 0;
        _lastJoinTime = 0;
        return;
      }

      _joined = true;
      _awaitingRouteResponse = false;
      _routeQueryDeadline = 0;
      _txHoldUntil = 0;
      _missedHelloAcks = 0;
      _lastRouteQueryTime = 0;
    }
    else
    {
      _joined = false;
      _token[0] = '\0';
      _awaitingRouteResponse = false;
      _routeQueryDeadline = 0;
      _txHoldUntil = 0;
      _missedHelloAcks = 0;
    }
  }
  else if (contains(incoming, "\"type\":\"hello_ack\""))
  {
    char targetId[32];
    targetId[0] = '\0';
    extractJsonString(incoming, "target_id", targetId, sizeof(targetId));
    if (targetId[0] && strcmp(targetId, _config.nodeId) != 0)
    {
      return;
    }

    _awaitingRouteResponse = false;
    _routeQueryDeadline = 0;
    _txHoldUntil = 0;
    _missedHelloAcks = 0;
  }
  else if (contains(incoming, "\"type\":\"route_resp\""))
  {
    // Route responses are informational only; ignore for liveness decisions.
  }
}

bool BridgeMesh::contains(const char *haystack, const char *needle)
{
  return strstr(haystack, needle) != nullptr;
}

int BridgeMesh::findJsonValueStart(const char *json, const char *key)
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

bool BridgeMesh::extractJsonBool(const char *json, const char *key)
{
  int start = findJsonValueStart(json, key);
  if (start < 0)
    return false;

  const char *p = json + start;
  return (strncmp(p, "true", 4) == 0) || (*p == '1');
}

bool BridgeMesh::extractJsonString(const char *json, const char *key, char *out, size_t outSize)
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

bool BridgeMesh::isLikelyJsonText(const uint8_t *buf, uint8_t len)
{
  if (!buf || len < 2)
    return false;

  uint8_t start = 0;
  while (start < len && (buf[start] == ' ' || buf[start] == '\t' || buf[start] == '\r' || buf[start] == '\n'))
    start++;

  if (strstr((const char*)buf + start, "\"type\":\"") == nullptr) return false;

  int end = len - 1;
  while (end >= 0 && (buf[end] == ' ' || buf[end] == '\t' || buf[end] == '\r' || buf[end] == '\n'))
    end--;

  return end >= 0 && buf[end] == '}';
}