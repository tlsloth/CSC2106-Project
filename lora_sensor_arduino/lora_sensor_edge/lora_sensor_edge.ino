#include <SPI.h>
#include <RH_RF95.h>
#include <DHT.h>
#include "BridgeMesh.h"

#define RFM95_CS 10
#define RFM95_RST 9
#define RFM95_INT 2
#define RF95_FREQ 920.0

#define DHTPIN 3
#define DHTTYPE DHT22

RH_RF95 rf95(RFM95_CS, RFM95_INT);
DHT dht(DHTPIN, DHTTYPE);

BridgeMeshConfig meshConfig = {
    "dht_sensor_A",
    "CSC2106_MESH",
    "mesh_key_v1",
    "dashboard_main", // targetDst
    10000UL,          // joinInterval
    15000UL,          // helloInterval
    3000UL            // helloAckTimeout
};

BridgeMesh mesh(rf95, meshConfig);

unsigned long lastTelemetryTime = 0;
const unsigned long TELEMETRY_INTERVAL = 5000UL;

bool sendTelemetry()
{
  float humidity = dht.readHumidity();
  float temperature = dht.readTemperature();
  if (isnan(humidity) || isnan(temperature))
    return false;

  return mesh.sendTelemetry(temperature, humidity);
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
    Serial.println("LoRa Init Failed");
    while (1)
    {
    }
  }

  if (!rf95.setFrequency(RF95_FREQ))
  {
    Serial.println("LoRa Freq Failed");
    while (1)
    {
    }
  }

  rf95.setTxPower(2, false);
  mesh.begin();
}

void loop()
{
  mesh.poll(20);

  mesh.tick();

  if (mesh.isJoined() && (millis() - lastTelemetryTime >= TELEMETRY_INTERVAL))
  {
    sendTelemetry();
    lastTelemetryTime = millis();
  }

  delay(50);
}