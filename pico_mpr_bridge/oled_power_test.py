# oled_power_test.py
# Simple OLED power/response test for Pico W without external dependencies.
# It scans common I2C pin mappings, finds OLED addresses (0x3C/0x3D),
# and sends SSD1306 display OFF/ON commands.

from machine import Pin, I2C
import time

# Common Pico W I2C pin pairs to try.
# Add your exact OLED SDA/SCL pins here first if known.
I2C_CANDIDATES = (
    (0, 0, 1),    # I2C0 on GP0/GP1
    (0, 4, 5),    # I2C0 on GP4/GP5
    (0, 8, 9),    # I2C0 on GP8/GP9
    (0, 12, 13),  # I2C0 on GP12/GP13
    (0, 16, 17),  # I2C0 on GP16/GP17
    (0, 20, 21),  # I2C0 on GP20/GP21
    (1, 2, 3),    # I2C1 on GP2/GP3
    (1, 6, 7),    # I2C1 on GP6/GP7
    (1, 10, 11),  # I2C1 on GP10/GP11
    (1, 14, 15),  # I2C1 on GP14/GP15
    (1, 18, 19),  # I2C1 on GP18/GP19
    (1, 26, 27),  # I2C1 on GP26/GP27
)

OLED_ADDRS = (0x3C, 0x3D)


def send_cmd(i2c, addr, cmd):
    # SSD1306 command prefix is 0x00.
    i2c.writeto(addr, bytes((0x00, cmd & 0xFF)))


def power_cycle_display(i2c, addr):
    print("  Sending SSD1306 power commands to 0x{:02X}".format(addr))
    send_cmd(i2c, addr, 0xAE)  # DISPLAYOFF
    time.sleep_ms(200)
    send_cmd(i2c, addr, 0xAF)  # DISPLAYON
    time.sleep_ms(200)


print("=== OLED Power Test (I2C scan + SSD1306 ON/OFF) ===")
found = False

for bus, sda_pin, scl_pin in I2C_CANDIDATES:
    try:
        i2c = I2C(bus, sda=Pin(sda_pin), scl=Pin(scl_pin), freq=400000)
        devices = i2c.scan()
        if not devices:
            continue

        print("I2C{} SDA=GP{} SCL=GP{} -> devices: {}".format(
            bus,
            sda_pin,
            scl_pin,
            ["0x{:02X}".format(d) for d in devices],
        ))

        for addr in devices:
            if addr in OLED_ADDRS:
                found = True
                print("  OLED-like device found at 0x{:02X}".format(addr))
                try:
                    power_cycle_display(i2c, addr)
                    print("  [PASS] Display acknowledged OFF/ON commands.")
                except Exception as e:
                    print("  [FAIL] Device found but command write failed:", e)

    except Exception:
        # Ignore invalid pin mappings / unavailable controller states.
        pass

if not found:
    print("No OLED found at 0x3C/0x3D on tested I2C pin pairs.")
    print("If you know your OLED SDA/SCL pins, add them at the top of this file.")
