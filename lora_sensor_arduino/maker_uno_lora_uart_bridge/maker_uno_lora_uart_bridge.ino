#include <SPI.h>
#include <RH_RF95.h>

// Maker UNO LoRa <-> UART bridge for Pico MPR bridge
// - Forwards LoRa RX frames to Pico in a format uart_lora_interface.py parses
// - Accepts Pico "LORA_TX|..." lines and transmits payload over LoRa

// LoRa Shield Pins
#define RFM95_CS 10
#define RFM95_RST 9
#define RFM95_INT 2
#define RF95_FREQ 920.0

// UART to Pico W
#define PICO_BAUD 9600

// Serial routing:
// - BRIDGE_UART is the only UART used for Pico bridge traffic.
// - Enable debug logs only when Pico is disconnected, otherwise logs will
//   corrupt bridge protocol lines.
#define ENABLE_DEBUG_LOGS 0
#define BRIDGE_UART Serial

#if ENABLE_DEBUG_LOGS
#define DBG_PRINT(x) Serial.print(x)
#define DBG_PRINTLN(x) Serial.println(x)
#define DBG_PRINTLN0() Serial.println()
#else
#define DBG_PRINT(x) \
  do                 \
  {                  \
  } while (0)
#define DBG_PRINTLN(x) \
  do                   \
  {                    \
  } while (0)
#define DBG_PRINTLN0() \
  do                   \
  {                    \
  } while (0)
#endif

// Protocol
#define PACKET_START 0xCB
#define MY_NODE_ID 'bridge_01'
#define MSG_TYPE_DATA 0x01
#define MSG_TYPE_ACK 0x02
#define HEADER_SIZE 6
#define MAX_PAYLOAD 20
#define ACK_DELAY_MS 40
// Lab mode: accept DATA packets regardless of destination ID.
// Set to false for strict destination filtering.
#define ACCEPT_ANY_DST true
#define UART_LINE_MAX 200

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

RH_RF95 rf95(RFM95_CS, RFM95_INT);

char uartLine[UART_LINE_MAX];
uint16_t uartLineLen = 0;

uint8_t calculateChecksum(Packet *pkt)
{
  uint8_t cs = pkt->start ^ pkt->from ^ pkt->to ^ pkt->type ^ pkt->seq ^ pkt->len;
  for (uint8_t i = 0; i < pkt->len; i++)
  {
    cs ^= pkt->payload[i];
  }
  return cs;
}

bool deserializePacket(uint8_t *buf, uint8_t bufLen, Packet *pkt)
{
  if (bufLen < HEADER_SIZE + 1)
    return false;
  if (buf[0] != PACKET_START)
    return false;

  pkt->start = buf[0];
  pkt->from = buf[1];
  pkt->to = buf[2];
  pkt->type = buf[3];
  pkt->seq = buf[4];
  pkt->len = buf[5];

  if (pkt->len > MAX_PAYLOAD)
    return false;
  if (bufLen < HEADER_SIZE + pkt->len + 1)
    return false;

  for (uint8_t i = 0; i < pkt->len; i++)
  {
    pkt->payload[i] = buf[HEADER_SIZE + i];
  }
  pkt->checksum = buf[HEADER_SIZE + pkt->len];

  return (calculateChecksum(pkt) == pkt->checksum);
}

bool isLikelyTextPayload(const uint8_t *buf, uint8_t len)
{
  for (uint8_t i = 0; i < len; i++)
  {
    uint8_t c = buf[i];
    bool printableAscii = (c >= 32 && c <= 126);
    bool allowedCtrl = (c == '\r' || c == '\n' || c == '\t');
    if (!printableAscii && !allowedCtrl)
    {
      return false;
    }
  }
  return true;
}

bool startsWith(const char *text, const char *prefix)
{
  while (*prefix)
  {
    if (*text++ != *prefix++)
    {
      return false;
    }
  }
  return true;
}

char *findNthChar(char *text, char needle, uint8_t occurrence)
{
  uint8_t count = 0;
  while (text && *text)
  {
    if (*text == needle)
    {
      count++;
      if (count == occurrence)
      {
        return text;
      }
    }
    text++;
  }
  return NULL;
}

void sendPicoLine(const char *line)
{
  BRIDGE_UART.println(line);
}

void sendRawLoRaPayload(const char *payload)
{
  if (!payload || !payload[0])
  {
    DBG_PRINTLN(F("TX LoRa skipped: empty payload"));
    return;
  }

  size_t payloadLen = strlen(payload);
  DBG_PRINT(F("TX LoRa start len="));
  DBG_PRINTLN((unsigned int)payloadLen);

  delay(5);
  rf95.send((uint8_t *)payload, payloadLen);
  bool txOk = rf95.waitPacketSent(3000);
  if (!txOk)
  {
    DBG_PRINTLN(F("TX LoRa TIMEOUT - resetting LoRa"));
    // Force LoRa back to idle/RX so the bridge doesn't stay wedged.
    rf95.setModeIdle();
  }
  delay(5);
  rf95.setModeRx();
  if (txOk)
  {
    DBG_PRINT(F("TX LoRa sent: "));
    DBG_PRINTLN(payload);
  }
  else
  {
    DBG_PRINT(F("TX LoRa failed: "));
    DBG_PRINTLN(payload);
  }
}

void sendJoinAckFromBridge(bool accepted, const char *bridgeId, const char *nodeId)
{
  (void)nodeId;
  const char payload[] =
      "{\"type\":\"join_ack\",\"accepted\":true,\"bridge_id\":\"bridge_01\"}";

  size_t payloadLen = strlen(payload);
  DBG_PRINT(F("Bridge TX test len="));
  DBG_PRINTLN((unsigned int)payloadLen);
  DBG_PRINTLN("Join request acknowleding for you boss!");
  rf95.setModeIdle();
  rf95.send((uint8_t *)payload, payloadLen);

  unsigned long start = millis();
  bool txOk = false;
  while (!rf95.waitPacketSent(100))
  {
    if (millis() - start > 3000)
    {
      break;
    }
  }
  txOk = (millis() - start <= 3000);

  rf95.setModeRx();

  DBG_PRINT(F("Bridge TX test result="));
  DBG_PRINTLN(txOk ? F("OK") : F("TIMEOUT"));
}

void forwardLegacyPacketToPico(Packet *pkt, int16_t rssi)
{
  char raw[MAX_PAYLOAD + 1];
  memcpy(raw, pkt->payload, pkt->len);
  raw[pkt->len] = '\0';

  // JSON line expected by pico_mpr_bridge/interfaces/uart_lora_interface.py
  char json[120];
  snprintf(
      json,
      sizeof(json),
      "{\"raw\":\"%s\",\"node\":\"%c\",\"rssi\":%d}",
      raw,
      (char)pkt->from,
      (int)rssi);

  sendPicoLine(json);
  DBG_PRINT(F("Forwarded legacy: "));
  DBG_PRINTLN(json);
}

void forwardRawFrameToPico(uint8_t *buf, uint8_t len, int16_t rssi)
{
  // Write directly to picoSerial in pieces to avoid large stack buffers.
  // Format: LORA_RX|rssi|0|payload\n
  char prefix[24];
  snprintf(prefix, sizeof(prefix), "LORA_RX|%d|0|", (int)rssi);
  BRIDGE_UART.print(prefix);
  BRIDGE_UART.write(buf, len);
  BRIDGE_UART.println();

  DBG_PRINT(F("Forwarded raw len="));
  DBG_PRINT((unsigned int)len);
  DBG_PRINT(F(" rssi="));
  DBG_PRINTLN((int)rssi);
}

void processPicoLine(char *line)
{
  if (!line || !line[0])
  {
    return;
  }

  DBG_PRINT(F("UART<-Pico: "));
  DBG_PRINTLN(line);

  if (startsWith(line, "LORA_TX|"))
  {
    char *payload = line + 8;
    sendRawLoRaPayload(payload);
    return;
  }

  if (startsWith(line, "LORA_JOIN_ACK|"))
  {
    char *acceptedText = line + 14;
    char *sep1 = findNthChar(acceptedText, '|', 1);
    if (!sep1)
    {
      DBG_PRINTLN(F("Invalid LORA_JOIN_ACK command"));
      return;
    }
    *sep1 = '\0';

    char *bridgeId = sep1 + 1;
    char *sep2 = findNthChar(bridgeId, '|', 1);
    if (!sep2)
    {
      DBG_PRINTLN(F("Invalid LORA_JOIN_ACK bridge id"));
      return;
    }
    *sep2 = '\0';

    char *nodeId = sep2 + 1;
    bool accepted = (acceptedText[0] == '1');
    sendJoinAckFromBridge(accepted, bridgeId, nodeId);
    return;
  }

  DBG_PRINT(F("Ignored UART line: "));
  DBG_PRINTLN(line);
}

void pollPicoUart()
{
  while (BRIDGE_UART.available())
  {
    char c = (char)BRIDGE_UART.read();
    if (c == '\r')
    {
      continue;
    }

    if (c == '\n')
    {
      uartLine[uartLineLen] = '\0';
      processPicoLine(uartLine);
      uartLineLen = 0;
      continue;
    }

    if (uartLineLen < (UART_LINE_MAX - 1))
    {
      uartLine[uartLineLen++] = c;
    }
    else
    {
      // Drop overlong line and reset buffer.
      DBG_PRINTLN(F("UART line overflow, dropping"));
      uartLineLen = 0;
    }
  }
}

void pollLoraRx()
{
  if (!rf95.available())
  {
    return;
  }

  uint8_t buf[RH_RF95_MAX_MESSAGE_LEN];
  uint8_t len = sizeof(buf);

  if (!rf95.recv(buf, &len))
  {
    DBG_PRINTLN(F("LoRa RX indicated available but recv failed"));
    return;
  }

  int16_t rssi = rf95.lastRssi();
  DBG_PRINT(F("LoRa RX len="));
  DBG_PRINT((unsigned int)len);
  DBG_PRINT(F(" rssi="));
  DBG_PRINTLN((int)rssi);
  Packet pkt;

  if (deserializePacket(buf, len, &pkt))
  {
    bool forThisBridge = (pkt.to == MY_NODE_ID || pkt.to == 0xFF);
    bool shouldProcess = (pkt.type == MSG_TYPE_DATA) && (forThisBridge || ACCEPT_ANY_DST);

    if (shouldProcess)
    {
      sendAck(&pkt);
      forwardLegacyPacketToPico(&pkt, rssi);
    }
    else if (pkt.type == MSG_TYPE_DATA)
    {
      DBG_PRINT(F("Data packet ignored (dst="));
      DBG_PRINT((char)pkt.to);
      DBG_PRINTLN(F(")"));
    }
    return;
  }

  // If it looks like our legacy binary framing but checksum/length is bad,
  // drop it instead of forwarding binary garbage as text to Pico.
  if (len >= (HEADER_SIZE + 1) && buf[0] == PACKET_START)
  {
    DBG_PRINTLN(F("Dropped malformed legacy-style packet"));
    return;
  }

  // Forward only text-like payloads on the LORA_RX text channel.
  if (!isLikelyTextPayload(buf, len))
  {
    DBG_PRINTLN(F("Dropped non-text raw LoRa payload"));
    return;
  }

  // Not a legacy binary packet -> treat as raw/text LoRa payload.
  forwardRawFrameToPico(buf, len, rssi);
}

void sendAck(Packet *rxPkt)
{
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
      ack.type, ack.seq, ack.len, ack.checksum};

  // Delay ACK slightly so sender has time to switch from TX to RX.
  delay(ACK_DELAY_MS);

  rf95.send(buf, sizeof(buf));
  if (!rf95.waitPacketSent(1500))
  {
    DBG_PRINTLN(F("ACK TX TIMEOUT - resetting LoRa"));
    rf95.setModeIdle();
  }
  rf95.setModeRx();

  DBG_PRINTLN(F("ACK sent"));
}

void setup()
{
  BRIDGE_UART.begin(PICO_BAUD);
  delay(100);
  DBG_PRINTLN(F("=== LoRa-UART Bridge (RH_RF95) ==="));

  pinMode(RFM95_RST, OUTPUT);
  digitalWrite(RFM95_RST, HIGH);
  delay(10);
  digitalWrite(RFM95_RST, LOW);
  delay(10);
  digitalWrite(RFM95_RST, HIGH);
  delay(10);

  if (!rf95.init())
  {
    DBG_PRINTLN(F("LoRa FAILED"));
    while (1)
    {
    }
  }

  if (!rf95.setFrequency(RF95_FREQ))
  {
    DBG_PRINTLN(F("Freq FAILED"));
    while (1)
    {
    }
  }

  rf95.setTxPower(13, false);
  rf95.setModeRx();
  DBG_PRINTLN(F("Ready - bridging LoRa <-> UART"));
}

void loop()
{
  pollPicoUart();
  pollLoraRx();
}
