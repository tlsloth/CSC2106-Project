# lora_smoketest.py
# Minimal RF96/SX127x responsiveness test for Pico W + MicroPython.
# Uses pin mapping from config.py.

import time
import config

REG_OP_MODE = 0x01
REG_VERSION = 0x42
MODE_LONG_RANGE_MODE = 0x80
MODE_SLEEP = 0x00
MODE_STDBY = 0x01


class SPIShim:
    """Collapse sx127x two-call transfer into one SPI frame when needed."""

    def __init__(self, spi):
        self._spi = spi
        self._pending = None

    def write(self, data):
        self._pending = bytes(data)

    def write_readinto(self, write_buf, read_buf):
        if self._pending is not None:
            tx = self._pending + bytes(write_buf)
            rx = bytearray(len(tx))
            self._spi.write_readinto(tx, rx)
            offset = len(self._pending)
            for i in range(len(read_buf)):
                read_buf[i] = rx[offset + i]
            self._pending = None
        else:
            self._spi.write_readinto(write_buf, read_buf)


def read_reg(spi, cs_pin, reg):
    tx = bytearray([reg & 0x7F, 0x00])
    rx = bytearray(2)
    cs_pin.value(0)
    spi.write_readinto(tx, rx)
    cs_pin.value(1)
    return rx[1], rx


def write_reg(spi, cs_pin, reg, value):
    tx = bytearray([reg | 0x80, value & 0xFF])
    rx = bytearray(2)
    cs_pin.value(0)
    spi.write_readinto(tx, rx)
    cs_pin.value(1)


def basic_register_test(spi, cs_pin, reset_pin):
    print("[TEST] Reset + REG_VERSION probe")
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

    if 0x12 not in seen:
        print("[FAIL] SX127x not detected (expected version 0x12).")
        return False

    print("[PASS] SX127x version detected.")

    print("[TEST] OP_MODE read/write sanity")
    # Sleep + LoRa bit
    write_reg(spi, cs_pin, REG_OP_MODE, MODE_LONG_RANGE_MODE | MODE_SLEEP)
    time.sleep_ms(5)
    op1, _ = read_reg(spi, cs_pin, REG_OP_MODE)
    print("  OP_MODE after sleep write: 0x{:02X}".format(op1))

    # Standby + LoRa bit
    write_reg(spi, cs_pin, REG_OP_MODE, MODE_LONG_RANGE_MODE | MODE_STDBY)
    time.sleep_ms(5)
    op2, _ = read_reg(spi, cs_pin, REG_OP_MODE)
    print("  OP_MODE after standby write: 0x{:02X}".format(op2))

    if (op1 & 0x80) == 0 or (op2 & 0x80) == 0:
        print("[FAIL] LoRa mode bit did not stick in OP_MODE.")
        return False

    print("[PASS] OP_MODE register responds.")
    return True


def sx127x_library_test(spi):
    print("[TEST] sx127x library init")
    try:
        from sx127x import SX127x

        pins = {
            "ss": config.LORA_PIN_CS,
            "reset": config.LORA_PIN_RESET,
            "dio_0": config.LORA_PIN_DIO0,
        }
        params = {
            "frequency": int(config.LORA_FREQ * 1e6),
            "tx_power_level": config.LORA_TX_POWER,
            "signal_bandwidth": config.LORA_BW * 1e3,
            "spreading_factor": config.LORA_SF,
            "coding_rate": config.LORA_CR,
            "preamble_length": 8,
            "implicitHeader": False,
            "sync_word": config.LORA_SYNC_WORD,
            "enable_CRC": True,
        }

        radio = SX127x(SPIShim(spi), pins, params)
        radio.receive()
        print("[PASS] sx127x initialized and entered RX mode.")
        return True
    except Exception as e:
        print("[FAIL] sx127x init failed:", e)
        return False


def main():
    from machine import SPI, Pin

    print("=== LoRa Smoke Test (RF96/SX127x) ===")
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

    ok_basic = basic_register_test(spi, cs_pin, reset_pin)
    ok_lib = sx127x_library_test(spi) if ok_basic else False

    if ok_basic and ok_lib:
        print("\nRESULT: PASS - LoRa radio is responsive.")
    elif ok_basic:
        print("\nRESULT: PARTIAL - chip responds, library init still failing.")
    else:
        print("\nRESULT: FAIL - chip not responding on SPI.")


main()
