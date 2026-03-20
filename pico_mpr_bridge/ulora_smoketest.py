# ulora_smoketest.py
# Validate RF96 responsiveness using local ulora stack.
# This test is intentionally standalone and does not depend on main.py.

import time
import config

REG_VERSION = 0x42


def read_reg(spi, cs_pin, reg):
    tx = bytearray([reg & 0x7F, 0x00])
    rx = bytearray(2)
    cs_pin.value(0)
    spi.write_readinto(tx, rx)
    cs_pin.value(1)
    return rx[1], rx


def probe_version(spi, cs_pin, reset_pin):
    print("[STEP 1] Direct REG_VERSION probe")
    reset_pin.value(0)
    time.sleep_ms(20)
    reset_pin.value(1)
    time.sleep_ms(100)

    seen = []
    for _ in range(8):
        version, raw = read_reg(spi, cs_pin, REG_VERSION)
        seen.append(version)
        print("  REG_VERSION=0x{:02X} raw=[0x{:02X},0x{:02X}]".format(version, raw[0], raw[1]))
        time.sleep_ms(10)

    ok = 0x12 in seen
    print("  Result:", "PASS" if ok else "FAIL")
    return ok


def test_ulora_init():
    print("[STEP 2] uLoRa import + constructor test")

    try:
        from ulora import TTN, uLoRa
    except Exception as e:
        print("  [FAIL] Could not import ulora:", e)
        print("  Hint: ulora.py imports ulora_encryption at module import time.")
        return False

    # Dummy values for constructor path.
    dev_addr = bytearray([0x26, 0x01, 0x1B, 0xAA])
    net_key = bytearray([0x00] * 16)
    app_key = bytearray([0x11] * 16)

    ttn = TTN(dev_addr, net_key, app_key, country="EU")

    try:
        radio = uLoRa(
            cs=config.LORA_PIN_CS,
            sck=config.LORA_PIN_SCK,
            mosi=config.LORA_PIN_MOSI,
            miso=config.LORA_PIN_MISO,
            irq=config.LORA_PIN_DIO0,
            rst=config.LORA_PIN_RESET,
            ttn_config=ttn,
            datarate="SF7BW125",
            fport=1,
            channel=None,
        )
        print("  [PASS] uLoRa constructor returned.")
        return True
    except Exception as e:
        print("  [FAIL] uLoRa constructor failed:", e)
        msg = str(e)
        if "ttn_" in msg or "No module named" in msg:
            print("  Hint: ulora frequency-plan helper modules (ttn_eu/ttn_as/...) may be missing.")
        return False


def main():
    from machine import SPI, Pin

    print("=== uLoRa Smoke Test ===")
    print("SPI{} SCK={} MOSI={} MISO={} CS={} RST={} DIO0={}".format(
        config.LORA_SPI_ID,
        config.LORA_PIN_SCK,
        config.LORA_PIN_MOSI,
        config.LORA_PIN_MISO,
        config.LORA_PIN_CS,
        config.LORA_PIN_RESET,
        config.LORA_PIN_DIO0,
    ))

    spi = SPI(
        config.LORA_SPI_ID,
        baudrate=500000,
        polarity=0,
        phase=0,
        sck=Pin(config.LORA_PIN_SCK),
        mosi=Pin(config.LORA_PIN_MOSI),
        miso=Pin(config.LORA_PIN_MISO),
    )

    cs_pin = Pin(config.LORA_PIN_CS, Pin.OUT)
    reset_pin = Pin(config.LORA_PIN_RESET, Pin.OUT)
    Pin(config.LORA_PIN_DIO0, Pin.IN)
    cs_pin.value(1)
    reset_pin.value(1)

    ok_spi = probe_version(spi, cs_pin, reset_pin)
    ok_ulora = test_ulora_init()

    if ok_spi and ok_ulora:
        print("\nRESULT: PASS - SPI response and uLoRa init are both working.")
    elif ok_spi:
        print("\nRESULT: PARTIAL - chip responds, but uLoRa dependency/init path failed.")
    else:
        print("\nRESULT: FAIL - chip not responding on SPI; library swap will not fix this yet.")


main()
