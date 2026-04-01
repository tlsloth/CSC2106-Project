#include <SPI.h>
#include <RH_RF95.h>

// Maker UNO LoRa <-> UART bridge for Pico MPR bridge
// - Receives raw LoRa binary and forwards it as a Hex String to Pico
// - Receives Hex Strings from Pico, packs to binary, and transmits over LoRa

#define RFM95_CS 10
#define RFM95_RST 9
#define RFM95_INT 2
#define RF95_FREQ 920.0

#define PICO_BAUD 9600
#define BRIDGE_UART Serial

#pragma once

// A 255-byte LoRa payload requires 510 hex characters + prefixes
#define UART_LINE_MAX 550

RH_RF95 rf95(RFM95_CS, RFM95_INT);

char uartLine[UART_LINE_MAX];
uint16_t uartLineLen = 0;
unsigned long loraRxCount = 0;
unsigned long loraTxCount = 0;
unsigned long uartOverflowCount = 0;

bool startsWith(const char *text, const char *prefix)
{
  while (*prefix)
  {
    if (*text++ != *prefix++)
      return false;
  }
  return true;
}

// Fast char-to-nibble converter
uint8_t hexCharToBin(char c)
{
  if (c >= '0' && c <= '9')
    return c - '0';
  if (c >= 'a' && c <= 'f')
    return c - 'a' + 10;
  if (c >= 'A' && c <= 'F')
    return c - 'A' + 10;
  return 0;
}

//==================== TX: PICO -> UNO (Hex String -> Binary LoRa) ====================//
void sendHexLoRaPayload(const char *hexPayload)
{
  size_t hexLen = strlen(hexPayload);

  // Must be even number of characters to form full bytes
  if (hexLen == 0 || hexLen % 2 != 0)
    return;

  size_t binLen = hexLen / 2;
  if (binLen > RH_RF95_MAX_MESSAGE_LEN)
    return; // Too big for radio!

  uint8_t binBuf[RH_RF95_MAX_MESSAGE_LEN];

  // Pack the hex string back into raw bytes
  for (size_t i = 0; i < binLen; i++)
  {
    binBuf[i] = (hexCharToBin(hexPayload[i * 2]) << 4) | hexCharToBin(hexPayload[i * 2 + 1]);
  }

  delay(5);
  rf95.send(binBuf, binLen);
  bool txOk = rf95.waitPacketSent(3000);

  if (!txOk)
  {
    // HARDWARE RECOVERY: The SX1276 chip has wedged. Reboot the silicon.
    digitalWrite(RFM95_RST, LOW);
    delay(10);
    digitalWrite(RFM95_RST, HIGH);
    delay(10);
    rf95.init();
    rf95.setFrequency(RF95_FREQ);
    rf95.setTxPower(2, false); // Keep at 2dBm for desk testing!
  }

  BRIDGE_UART.println("LORA_STATUS|TX_DONE");
  delay(5);
  rf95.setModeRx();
}

void processPicoLine(char *line)
{
  if (!line || !line[0])
    return;

  if (startsWith(line, "LORA_TX|"))
  {
    char *hexPayload = line + 8;
    sendHexLoRaPayload(hexPayload);
  }
}

void pollPicoUart()
{
  while (BRIDGE_UART.available())
  {
    char c = (char)BRIDGE_UART.read();
    if (c == '\r')
      continue;

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
      uartOverflowCount++;
      uartLineLen = 0;
    }
  }
}

//==================== RX: UNO -> PICO (Binary LoRa -> Hex String) ====================//
void forwardRawFrameToPico(uint8_t *buf, uint8_t len, int16_t rssi)
{
  loraRxCount++;

  BRIDGE_UART.print("LORA_RX|");
  BRIDGE_UART.print(rssi);
  BRIDGE_UART.print("|0|");

  // Safely translate raw bytes into a Hex string so it doesn't break UART newlines
  for (int i = 0; i < len; i++)
  {
    if (buf[i] < 16)
      BRIDGE_UART.print("0"); // Add leading zero
    BRIDGE_UART.print(buf[i], HEX);
  }
  BRIDGE_UART.println();
}

void pollLoraRx()
{
  if (!rf95.available())
    return;

  uint8_t buf[RH_RF95_MAX_MESSAGE_LEN];
  uint8_t len = sizeof(buf);

  if (!rf95.recv(buf, &len))
    return;

  int16_t rssi = rf95.lastRssi();
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
    while (1)
      ;
  if (!rf95.setFrequency(RF95_FREQ))
    while (1)
      ;

  rf95.setTxPower(2, false); // 2dBm for desk testing
  rf95.setModeRx();
}

void loop()
{
  pollPicoUart();
  pollLoraRx();
}