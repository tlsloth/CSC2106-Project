#ifndef BRIDGE_MESH_H
#define BRIDGE_MESH_H

#include <Arduino.h>
#include <RH_RF95.h>

struct BridgeMeshConfig
{
  const char *nodeId;
  const char *networkName;
  const char *joinKey;
  const char *routeDst;

  unsigned long joinInterval;
  unsigned long helloInterval;
  unsigned long routeQueryInterval;
  unsigned long routeQueryTimeout;
};

class BridgeMesh
{
public:
  BridgeMesh(RH_RF95 &radio, const BridgeMeshConfig &config);

  bool begin();
  void poll(unsigned long maxMs);
  void tick();

  bool sendJoinRequest();
  bool sendHello();
  bool sendRouteQuery(const char *dst);
  bool sendJsonObject(const char *jsonObject, const char *type);

  bool isJoined() const;
  const char *token() const;
  const char *bridgeId() const;
  const BridgeMeshConfig &config() const;

private:
  RH_RF95 &_radio;
  BridgeMeshConfig _config;

  char _token[48];      // decrypted token (plain hex)
  char _bridgeId[20];

  bool _joined;
  bool _awaitingRouteResponse;
  uint8_t _seq;

  unsigned long _lastJoinTime;
  unsigned long _lastHelloTime;
  unsigned long _lastRouteQueryTime;
  unsigned long _routeQueryDeadline;

  bool sendRaw(const char *payload);
  void handleControlMessage(const char *incoming);

  static bool contains(const char *haystack, const char *needle);
  static int findJsonValueStart(const char *json, const char *key);
  static bool extractJsonBool(const char *json, const char *key);
  static bool extractJsonString(const char *json, const char *key, char *out, size_t outSize);
  static bool isLikelyJsonText(const uint8_t *buf, uint8_t len);

  static uint32_t fnv1a32(const char *str);
  static bool decryptToken(const char *encoded, const char *key, char *out, size_t outSize);
};

#endif