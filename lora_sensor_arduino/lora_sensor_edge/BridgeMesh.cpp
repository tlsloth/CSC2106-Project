#include "BridgeMesh.h"
#include <string.h>
#include <stdio.h>

static const unsigned long HELLO_RX_GUARD_MS = 2000UL;
static const unsigned long HELLO_RETRY_AFTER_MISS_MS = 1200UL;
static const uint8_t HELLO_ACK_MISS_THRESHOLD = 3;

BridgeMesh::BridgeMesh(RH_RF95 &radio, const BridgeMeshConfig &config)
    : _radio(radio),
      _config(config),
      _joined(false),
      _awaitingHelloAck(false),
      _missedHelloAcks(0),
      _seq(0),
      _lastJoinTime(0),
      _lastHelloTime(0),
      _helloAckDeadline(0),
      _txHoldUntil(0)
{
  memset(_token, 0, sizeof(_token));
  strncpy(_bridgeId, "bridge_01", sizeof(_bridgeId) - 1);
  _bridgeId[sizeof(_bridgeId) - 1] = '\0';
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

void BridgeMesh::xorTokenBytes(const uint8_t *in, uint8_t *out)
{
  size_t keyLen = strlen(_config.joinKey);
  if (keyLen == 0)
    return;
  for (int i = 0; i < 8; i++)
  {
    out[i] = in[i] ^ (uint8_t)_config.joinKey[i % keyLen];
  }
}

bool BridgeMesh::begin()
{
  _joined = false;
  _awaitingHelloAck = false;
  _missedHelloAcks = 0;
  _seq = 0;
  _lastJoinTime = 0;
  _lastHelloTime = 0;
  _helloAckDeadline = 0;
  _txHoldUntil = 0;
  memset(_token, 0, sizeof(_token));
  randomSeed(micros());
  return sendJoinRequest();
}

const char *BridgeMesh::bridgeId() const
{
  return _bridgeId;
}

bool BridgeMesh::isJoined() const
{
  return _joined;
}

bool BridgeMesh::sendJoinRequest()
{
  Serial.println("TX| JoinReq (Binary)");
  LoRaJoinReq req = {0};
  req.type = 0x00;
  strncpy(req.node_id, _config.nodeId, 15);
  strncpy(req.network, _config.networkName, 15);
  req.auth = fnv1a32(_config.joinKey);
  req.seq = _seq++;

  _radio.send((const uint8_t *)&req, sizeof(req));
  _radio.waitPacketSent();
  _radio.setModeRx();
  _lastJoinTime = millis();
  return true;
}

bool BridgeMesh::sendHello()
{
  if (!_joined)
    return false;
  Serial.println("TX| Hello (Binary)");

  LoRaHello hello = {0};
  hello.type = 0x02;
  strncpy(hello.node_id, _config.nodeId, 15);
  strncpy(hello.network, _config.networkName, 15);
  xorTokenBytes(_token, hello.token);
  hello.seq = _seq++;

  _radio.send((const uint8_t *)&hello, sizeof(hello));
  _radio.waitPacketSent();
  _radio.setModeRx();

  unsigned long now = millis();
  _lastHelloTime = now;
  _awaitingHelloAck = true;
  _helloAckDeadline = now + _config.helloAckTimeout;
  _txHoldUntil = now + HELLO_RX_GUARD_MS;
  return true;
}

bool BridgeMesh::sendTelemetry(float temp, float hum)
{
  if (!_joined)
    return false;

  if ((long)(millis() - _txHoldUntil) < 0)
  {
    return false;
  }

  Serial.println("TX| Telemetry (Binary)");

  LoRaTelemetry tel = {0};
  tel.type = 0x04;
  strncpy(tel.node_id, _config.nodeId, 15);
  strncpy(tel.hop_dst, _bridgeId, 15);
  strncpy(tel.dst, _config.targetDst, 15);
  xorTokenBytes(_token, tel.token);

  // Scale floats to integers to save bytes!
  tel.temp = (int16_t)(temp * 10.0);
  tel.hum = (uint16_t)(hum * 10.0);

  _radio.send((const uint8_t *)&tel, sizeof(tel));
  _radio.waitPacketSent();
  _radio.setModeRx();
  return true;
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

  if (!_awaitingHelloAck && (_lastHelloTime == 0 || (now - _lastHelloTime >= _config.helloInterval)))
  {
    sendHello();
  }

  if (_awaitingHelloAck && (long)(now - _helloAckDeadline) >= 0)
  {
    _awaitingHelloAck = false;
    _helloAckDeadline = 0;
    _txHoldUntil = 0;
    _missedHelloAcks++;

    if (_missedHelloAcks >= HELLO_ACK_MISS_THRESHOLD)
    {
      _joined = false;
      memset(_token, 0, sizeof(_token));
      _lastJoinTime = 0;
      _missedHelloAcks = 0;
    }
    else
    {
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
    {
      delay(1);
      continue;
    }

    uint8_t recvBuf[RH_RF95_MAX_MESSAGE_LEN];
    uint8_t recvLen = sizeof(recvBuf);

    if (_radio.recv(recvBuf, &recvLen))
    {
      handleControlMessage(recvBuf, recvLen);
    }
  }
}

void BridgeMesh::handleControlMessage(const uint8_t *incoming, uint8_t len)
{
  if (len == 0)
    return;

  // 0x01 = Join Ack
  if (incoming[0] == 0x01 && len == sizeof(LoRaJoinAck))
  {
    LoRaJoinAck *ack = (LoRaJoinAck *)incoming;
    if (strcmp(ack->target_id, _config.nodeId) != 0)
      return;

    if (ack->accepted)
    {
      strncpy(_bridgeId, ack->bridge_id, 15);
      _bridgeId[15] = '\0';
      xorTokenBytes(ack->token, _token);
      _joined = true;
      _awaitingHelloAck = false;
      _helloAckDeadline = 0;
      _txHoldUntil = 0;
      _missedHelloAcks = 0;
      _lastHelloTime = millis();
      Serial.println("RX| Join Accepted!");
    }
    else
    {
      _joined = false;
      memset(_token, 0, sizeof(_token));
      _awaitingHelloAck = false;
      _helloAckDeadline = 0;
      _txHoldUntil = 0;
      _missedHelloAcks = 0;
      Serial.println("RX| Join Rejected!");
    }
  }
  // 0x03 = Hello Ack
  else if (incoming[0] == 0x03 && len == sizeof(LoRaHelloAck))
  {
    LoRaHelloAck *ack = (LoRaHelloAck *)incoming;
    if (strcmp(ack->target_id, _config.nodeId) == 0)
    {
      _awaitingHelloAck = false;
      _helloAckDeadline = 0;
      _txHoldUntil = 0;
      _missedHelloAcks = 0;
      Serial.println("RX| Hello Ack!");
    }
  }
}