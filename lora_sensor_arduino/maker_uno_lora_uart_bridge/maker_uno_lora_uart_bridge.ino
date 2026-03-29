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
#define UART_LINE_MAX 200

RH_RF95 rf95(RFM95_CS, RFM95_INT);

char uartLine[UART_LINE_MAX];
uint16_t uartLineLen = 0;

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

void sendJoinAckFromBridge(bool accepted, const char *bridgeId, const char *nodeId, const char *token)
{
  (void)nodeId;
  char payload[220];
  if (token && token[0])
  {
    snprintf(
        payload,
        sizeof(payload),
        "{\"type\":\"join_ack\",\"accepted\":%s,\"bridge_id\":\"%s\",\"token\":\"%s\"}",
        accepted ? "true" : "false",
        (bridgeId && bridgeId[0]) ? bridgeId : "bridge_01",
        token);
  }
  else
  {
    snprintf(
        payload,
        sizeof(payload),
        "{\"type\":\"join_ack\",\"accepted\":%s,\"bridge_id\":\"%s\"}",
        accepted ? "true" : "false",
        (bridgeId && bridgeId[0]) ? bridgeId : "bridge_01");
  }
  sendRawLoRaPayload(payload);
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
    char *token = (char *)"";
    char *sep3 = findNthChar(nodeId, '|', 1);
    if (sep3)
    {
      *sep3 = '\0';
      token = sep3 + 1;
    }
    bool accepted = (acceptedText[0] == '1');
    sendJoinAckFromBridge(accepted, bridgeId, nodeId, token);
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
  // Forward only text-like payloads on the LORA_RX text channel.
  if (!isLikelyTextPayload(buf, len))
  {
    DBG_PRINTLN(F("Dropped non-text raw LoRa payload"));
    return;
  }

  // JSON/text-only LoRa pipeline.
  forwardRawFrameToPico(buf, len, rssi);
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
