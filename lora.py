# main.py (LoRa test sender node for your MPR bridge)

import time
import json
from machine import ADC
from sx1262 import SX1262

# ---------- Node + LoRa config ----------
NODE_ID = "lora_test_01"

# Must match bridge config
LORA_FREQ = 923.0
LORA_BW = 125.0
LORA_SF = 9
LORA_CR = 5
LORA_SYNC_WORD = 0x12
LORA_TX_POWER = 14

# Pins based on your shield wiring
LORA_SPI_ID = 0
LORA_PIN_SCK = 18
LORA_PIN_MOSI = 19
LORA_PIN_MISO = 16
LORA_PIN_CS = 17
LORA_PIN_RESET = 20
LORA_PIN_DIO1 = 21
LORA_PIN_BUSY = 2  # change if your sensor node wiring differs

HELLO_INTERVAL_S = 15
DATA_INTERVAL_S = 5

# ---------- Helpers ----------
def read_temp_c():
    # Pico internal temp sensor (for test data)
    adc = ADC(4)
    raw = adc.read_u16()
    voltage = raw * (3.3 / 65535)
    temp_c = 27 - (voltage - 0.706) / 0.001721
    return round(temp_c, 2)

def send_json(sx, payload):
    data = json.dumps(payload).encode("utf-8")
    sx.send(data)
    print("TX:", payload)

# ---------- Main ----------
def main():
    print("Init SX1262...")
    sx = SX1262(
        LORA_SPI_ID,
        LORA_PIN_SCK,
        LORA_PIN_MOSI,
        LORA_PIN_MISO,
        LORA_PIN_CS,
        LORA_PIN_DIO1,
        LORA_PIN_RESET,
        LORA_PIN_BUSY,
    )

    sx.begin(
        freq=LORA_FREQ,
        bw=LORA_BW,
        sf=LORA_SF,
        cr=LORA_CR,
        syncWord=LORA_SYNC_WORD,
        power=LORA_TX_POWER,
        preambleLength=8,
        implicit=False,
    )

    print("LoRa ready, sending packets...")
    last_hello = 0
    seq = 0

    while True:
        now = time.time()

        # 1) Hello packet for neighbour discovery
        if now - last_hello >= HELLO_INTERVAL_S:
            hello = {
                "type": "hello",
                "node_id": NODE_ID,
                "role": "sensor",
                "capabilities": ["LoRa"],
                "timestamp": now
            }
            send_json(sx, hello)
            last_hello = now

        # 2) Telemetry packet for bridge translator
        temp_c = read_temp_c()
        pkt = {
            "src": NODE_ID,
            "dst": "dashboard",
            "hop_src": NODE_ID,
            "hop_dst": "bridge_01",   # set to your bridge NODE_ID
            "ttl": 5,
            "priority": 1 if temp_c >= 40 else 5,
            "seq": seq,
            "frag": {"index": 0, "total": 1},
            "payload": {"temp": temp_c}
        }
        send_json(sx, pkt)
        seq = (seq + 1) % 65536

        time.sleep(DATA_INTERVAL_S)

main()