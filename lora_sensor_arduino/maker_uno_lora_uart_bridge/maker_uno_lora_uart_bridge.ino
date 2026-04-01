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
#define BRIDGE_UART Serial

#pragma once

// Protocol
#define UART_LINE_MAX 200
#define STATUS_HEARTBEAT_MS 5000UL

RH_RF95 rf95(RFM95_CS, RFM95_INT);

char uartLine[UART_LINE_MAX];
uint16_t uartLineLen = 0;
unsigned long lastStatusHeartbeat = 0;
unsigned long loraRxCount = 0;
unsigned long loraTxCount = 0;
unsigned long uartCmdCount = 0;
unsigned long uartOverflowCount = 0;

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

void sendRawLoRaPayload(const char *payload)
{
  if (!payload || !payload[0])
  {
    return;
  }

  size_t payloadLen = strlen(payload);

  delay(5);
  rf95.send((uint8_t *)payload, payloadLen);
  bool txOk = rf95.waitPacketSent(3000);
  if (!txOk)
  {
    // Force LoRa back to idle/RX so the bridge doesn't stay wedged.
    rf95.setModeIdle();
  }
  Serial.println("LORA_STATUS|TX_DONE");
  delay(5);
  rf95.setModeRx();
}

// void sendJoinAckFromBridge(bool accepted, const char *bridgeId, const char *nodeId, const char *token)
// {
//   char payload[220];
//   if (token && token[0])
//   {
//     snprintf(
//         payload,
//         sizeof(payload),
//         "{\"type\":\"join_ack\",\"accepted\":%s,\"bridge_id\":\"%s\",\"target_id\":\"%s\",\"token\":\"%s\"}",
//         accepted ? "true" : "false",
//         (bridgeId && bridgeId[0]) ? bridgeId : "bridge_01",
//         (nodeId && nodeId[0]) ? nodeId : "unknown",
//         token);
//   }
//   else
//   {
//     snprintf(
//         payload,
//         sizeof(payload),
//         "{\"type\":\"join_ack\",\"accepted\":%s,\"bridge_id\":\"%s\",\"target_id\":\"%s\"}",
//         accepted ? "true" : "false",
//         (bridgeId && bridgeId[0]) ? bridgeId : "bridge_01",
//         (nodeId && nodeId[0]) ? nodeId : "unknown");
//   }
//   sendRawLoRaPayload(payload);
// }

//==================== PICO -> UNO ====================//
void processPicoLine(char *line)
{
  if (!line || !line[0])
  {
    return;
  }

  uartCmdCount++;

  if (startsWith(line, "LORA_TX|"))
  {
    char *payload = line + 8;
    sendRawLoRaPayload(payload);
    return;
  }
  // if required, other instructions can be added here
}

// function to constantly poll for messages to send from the pico
void pollPicoUart()
{
  while (BRIDGE_UART.available())
  {
    char c = (char)BRIDGE_UART.read();
    if (c == '\r')
    {
      continue;
    }
    // end of message from pico
    if (c == '\n')
    {
      uartLine[uartLineLen] = '\0';
      // do tx here
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
      uartOverflowCount++;
      uartLineLen = 0;
    }
  }
}

//==================== UNO -> PICO ====================//

// function to push lora data to pico
void forwardRawFrameToPico(uint8_t *buf, uint8_t len, int16_t rssi)
{
  loraRxCount++;
  // Write directly to picoSerial in pieces to avoid large stack buffers.
  // Format: LORA_RX|rssi|0|payload\n
  char prefix[24];
  snprintf(prefix, sizeof(prefix), "LORA_RX|%d|0|", (int)rssi);
  BRIDGE_UART.print(prefix);
  BRIDGE_UART.write(buf, len);
  BRIDGE_UART.println();
}

// receive lora packets from external sources
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
    return;
  }

  int16_t rssi = rf95.lastRssi();
  // JSON/text-only LoRa pipeline.
  forwardRawFrameToPico(buf, len, rssi);
}

void setup()
{
  BRIDGE_UART.begin(PICO_BAUD);
  delay(100);

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

  rf95.setTxPower(2, false);
  rf95.setModeRx();
}

void loop()
{
  pollPicoUart();
  pollLoraRx();
}
