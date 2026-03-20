# lora_probe.py - standalone RF96/SX127x wiring probe for Pico W MicroPython

from machine import SPI, Pin
import time

REG_VERSION = 0x42
EXPECTED = 0x12

# Candidate SPI routing sets for RP2040 and your project variants.
SPI_CANDIDATES = [
    {
        "name": "Current project map (SPI0 alt)",
        "spi_id": 0,
        "sck": 18,
        "mosi": 19,
        "miso": 16,
        "cs": 17,
        "rst": 20,
    },
    {
        "name": "README SX1262 map pins as SPI1-style",
        "spi_id": 1,
        "sck": 10,
        "mosi": 11,
        "miso": 12,
        "cs": 3,
        "rst": 15,
    },
]

BAUDS = (100000, 500000, 1000000)

# Extra candidates for shields where CS/RESET jumpers are moved or labels are misread.
CS_CANDIDATES = (17, 3, 10, 9, 5)
RST_CANDIDATES = (20, 15, 9, 22)


def read_reg(spi, cs, reg):
    tx = bytearray([reg & 0x7F, 0x00])
    rx = bytearray(2)
    cs.value(0)
    spi.write_readinto(tx, rx)
    cs.value(1)
    return rx[1], rx


def miso_pull_probe(pin_no):
    p_up = Pin(pin_no, Pin.IN, Pin.PULL_UP)
    time.sleep_ms(2)
    up = p_up.value()

    p_dn = Pin(pin_no, Pin.IN, Pin.PULL_DOWN)
    time.sleep_ms(2)
    dn = p_dn.value()

    Pin(pin_no, Pin.IN)

    if up == 0 and dn == 0:
        state = "stuck_low"
    elif up == 1 and dn == 1:
        state = "stuck_high"
    elif up == 1 and dn == 0:
        state = "floating_or_weakly_driven"
    else:
        state = "unexpected"

    return up, dn, state


def run_one(cfg, cs_override=None, rst_override=None, quick=False):
    cs_pin = cfg["cs"] if cs_override is None else cs_override
    rst_pin = cfg["rst"] if rst_override is None else rst_override

    print("\n=== {} ===".format(cfg["name"]))
    print("SPI{} SCK={} MOSI={} MISO={} CS={} RST={}".format(
        cfg["spi_id"], cfg["sck"], cfg["mosi"], cfg["miso"], cs_pin, rst_pin))

    up, dn, state = miso_pull_probe(cfg["miso"])
    print("MISO pull probe: up={} down={} -> {}".format(up, dn, state))

    try:
        spi = SPI(
            cfg["spi_id"],
            baudrate=BAUDS[0],
            polarity=0,
            phase=0,
            sck=Pin(cfg["sck"]),
            mosi=Pin(cfg["mosi"]),
            miso=Pin(cfg["miso"]),
        )
        cs = Pin(cs_pin, Pin.OUT)
        rst = Pin(rst_pin, Pin.OUT)
    except Exception as e:
        print("Init error:", e)
        return False

    baud_list = (100000,) if quick else BAUDS
    reads_per_baud = 2 if quick else 8

    for b in baud_list:
        print("\n  -- baud {} --".format(b))
        try:
            spi.init(baudrate=b, polarity=0, phase=0)

            rst.value(0)
            time.sleep_ms(20)
            rst.value(1)
            time.sleep_ms(80)

            seen = []
            for _ in range(reads_per_baud):
                v, raw = read_reg(spi, cs, REG_VERSION)
                seen.append(v)
                print("  read REG_VERSION: 0x{:02X} raw=[0x{:02X},0x{:02X}]".format(v, raw[0], raw[1]))
                time.sleep_ms(8)

            if EXPECTED in seen:
                print("  SUCCESS: detected RF96/SX127x version 0x12")
                return True
            print("  unique values:", ["0x{:02X}".format(x) for x in sorted(set(seen))])

        except Exception as e:
            print("  probe error:", e)

    return False


def main():
    print("RF96/SX127x probe starting")
    ok = False

    # Pass 1: strict configured candidates.
    for cfg in SPI_CANDIDATES:
        if run_one(cfg):
            ok = True
            break

    # Pass 2: brute-force CS/RESET alternatives on same SPI lines.
    if not ok:
        print("\nNo success in configured mappings. Scanning CS/RESET alternatives...")
        for cfg in SPI_CANDIDATES:
            for cs_pin in CS_CANDIDATES:
                for rst_pin in RST_CANDIDATES:
                    # Skip duplicate of base case already tested.
                    if cs_pin == cfg["cs"] and rst_pin == cfg["rst"]:
                        continue
                    if run_one(cfg, cs_override=cs_pin, rst_override=rst_pin, quick=True):
                        print("\nFOUND WORKING MAP:")
                        print("SPI{} SCK={} MOSI={} MISO={} CS={} RST={}".format(
                            cfg["spi_id"], cfg["sck"], cfg["mosi"], cfg["miso"], cs_pin, rst_pin))
                        ok = True
                        break
                if ok:
                    break
            if ok:
                break

    if not ok:
        print("\nNo valid 0x12 response found.")
        print("Likely causes: MISO path open/floating, radio unpowered, or damaged module.")


main()
