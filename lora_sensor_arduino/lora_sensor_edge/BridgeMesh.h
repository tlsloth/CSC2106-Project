#ifndef BRIDGE_MESH_H
#define BRIDGE_MESH_H

#include <Arduino.h>
#include <RH_RF95.h>

struct BridgeMeshConfig
{
  const char *nodeId;
  const char *networkName;
  const char *joinKey;
  const char *targetDst;

  unsigned long joinInterval;
  unsigned long helloInterval;
  unsigned long helloAckTimeout;
};

// --- THE NEW BINARY PACKET STRUCTS ---
#pragma pack(push, 1)
struct LoRaJoinReq { 
  uint8_t type; char node_id[16]; char network[16]; uint32_t auth; uint8_t seq; 
};
struct LoRaJoinAck { 
  uint8_t type; char target_id[16]; uint8_t accepted; char bridge_id[16]; uint8_t token[8]; 
};
struct LoRaHello { 
  uint8_t type; char node_id[16]; char network[16]; uint8_t token[8]; uint8_t seq; 
};
struct LoRaHelloAck { 
  uint8_t type; char target_id[16]; char bridge_id[16]; 
};
struct LoRaTelemetry { 
  uint8_t type; char node_id[16]; char hop_dst[16]; char dst[16]; uint8_t token[8]; int16_t temp; uint16_t hum; 
};
#pragma pack(pop)

class BridgeMesh
{
public:
  BridgeMesh(RH_RF95 &radio, const BridgeMeshConfig &config);

  bool begin();
  void poll(unsigned long maxMs);
  void tick();

  bool sendJoinRequest();
  bool sendHello();
  bool sendTelemetry(float temp, float hum); // <--- Replaced sendJsonObject!

  bool isJoined() const;
  const char *bridgeId() const;

private:
  RH_RF95 &_radio;
  BridgeMeshConfig _config;

  uint8_t _token[8]; // Raw bytes now!
  char _bridgeId[16];

  bool _joined;
  bool _awaitingHelloAck;
  uint8_t _missedHelloAcks;
  uint8_t _seq;

  unsigned long _lastJoinTime;
  unsigned long _lastHelloTime;
  unsigned long _helloAckDeadline;
  unsigned long _txHoldUntil;

  void handleControlMessage(const uint8_t *incoming, uint8_t len);
  void xorTokenBytes(const uint8_t *in, uint8_t *out);
  static uint32_t fnv1a32(const char *str);
};

#endif