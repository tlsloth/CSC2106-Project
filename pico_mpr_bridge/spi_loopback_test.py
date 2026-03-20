# spi_loopback_test.py - verify Pico SPI path independent of LoRa module
#
# Usage:
# 1) Disconnect LoRa module from SPI pins.
# 2) Add a temporary jumper from MOSI to MISO on the Pico.
# 3) Run: import spi_loopback_test

from machine import SPI, Pin
import time

CANDIDATES = [
    {"name": "SPI0 alt", "spi_id": 0, "sck": 18, "mosi": 19, "miso": 16},
    {"name": "SPI1 std", "spi_id": 1, "sck": 10, "mosi": 11, "miso": 12},
]

PATTERNS = [
    bytes([0x00]),
    bytes([0xFF]),
    bytes([0x55]),
    bytes([0xAA]),
    bytes([0x42]),
    bytes([0x12]),
    bytes([0xDE, 0xAD, 0xBE, 0xEF]),
]


def run_candidate(cfg):
    print("\n=== {} (SPI{}, SCK={}, MOSI={}, MISO={}) ===".format(
        cfg["name"], cfg["spi_id"], cfg["sck"], cfg["mosi"], cfg["miso"]))

    try:
        spi = SPI(
            cfg["spi_id"],
            baudrate=100000,
            polarity=0,
            phase=0,
            sck=Pin(cfg["sck"]),
            mosi=Pin(cfg["mosi"]),
            miso=Pin(cfg["miso"]),
        )
    except Exception as e:
        print("SPI init failed:", e)
        return False

    all_ok = True
    for baud in (100000, 500000, 1000000, 2000000):
        try:
            spi.init(baudrate=baud, polarity=0, phase=0)
            print("  baud {}".format(baud))

            for p in PATTERNS:
                rx = bytearray(len(p))
                spi.write_readinto(p, rx)
                ok = bytes(rx) == p
                print("    TX={} RX={} {}".format(
                    p.hex(), bytes(rx).hex(), "OK" if ok else "FAIL"))
                if not ok:
                    all_ok = False

                time.sleep_ms(3)
        except Exception as e:
            print("  transfer error:", e)
            all_ok = False

    return all_ok


def main():
    print("SPI loopback test (MOSI jumper to MISO required)")
    print("If all patterns pass, Pico SPI software path is good.")

    any_pass = False
    for cfg in CANDIDATES:
        ok = run_candidate(cfg)
        if ok:
            any_pass = True
            print("  RESULT: PASS")
        else:
            print("  RESULT: FAIL")

    if not any_pass:
        print("\nNo SPI candidate passed loopback. Recheck jumper and selected pins.")


main()
