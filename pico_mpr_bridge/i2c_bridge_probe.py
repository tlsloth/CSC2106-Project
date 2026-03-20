import time

import config

STATUS_MAGIC = 0xA5
REG_STATUS = 0x00


def candidate_i2c_configs():
    configured = (
        config.I2C_LORA_ID,
        config.I2C_LORA_SDA_PIN,
        config.I2C_LORA_SCL_PIN,
    )
    common = [
        (0, 0, 1),
        (0, 4, 5),
        (0, 8, 9),
        (0, 12, 13),
        (0, 16, 17),
        (0, 20, 21),
        (1, 2, 3),
        (1, 6, 7),
        (1, 10, 11),
        (1, 14, 15),
        (1, 18, 19),
        (1, 26, 27),
    ]

    result = [configured]
    for item in common:
        if item not in result:
            result.append(item)
    return result


def line_state(Pin, pin_number):
    pull_up = Pin(pin_number, Pin.IN, Pin.PULL_UP)
    up_val = pull_up.value()
    pull_down = Pin(pin_number, Pin.IN, Pin.PULL_DOWN)
    down_val = pull_down.value()
    Pin(pin_number, Pin.IN)

    if up_val == 0 and down_val == 0:
        verdict = "stuck_low"
    elif up_val == 1 and down_val == 1:
        verdict = "stuck_high"
    elif up_val == 1 and down_val == 0:
        verdict = "floating_or_pulled"
    else:
        verdict = "unexpected"

    return up_val, down_val, verdict


def try_status(i2c):
    # Some MicroPython builds reject keyword args for writeto(stop=...).
    i2c.writeto(config.I2C_LORA_ADDR, bytes([REG_STATUS]), False)
    status = i2c.readfrom(config.I2C_LORA_ADDR, 6)
    if status[0] != STATUS_MAGIC:
        raise OSError("invalid status {}".format(list(status)))
    return status


def main():
    from machine import I2C, Pin

    print("=== I2C LoRa Bridge Probe ===")
    print("Configured bus:", config.I2C_LORA_ID)
    print("Configured pins: SDA=GP{} SCL=GP{}".format(config.I2C_LORA_SDA_PIN, config.I2C_LORA_SCL_PIN))
    print("Expecting address: 0x{:02X}".format(config.I2C_LORA_ADDR))

    sda_up, sda_down, sda_state = line_state(Pin, config.I2C_LORA_SDA_PIN)
    scl_up, scl_down, scl_state = line_state(Pin, config.I2C_LORA_SCL_PIN)
    print("Configured line state: SDA up/down={}/{} {} | SCL up/down={}/{} {}".format(
        sda_up, sda_down, sda_state, scl_up, scl_down, scl_state))

    found = None
    for bus_id, sda_pin, scl_pin in candidate_i2c_configs():
        try:
            i2c = I2C(
                bus_id,
                scl=Pin(scl_pin),
                sda=Pin(sda_pin),
                freq=config.I2C_LORA_FREQ,
            )
            devices = i2c.scan()
            print("Scan I2C{} SDA=GP{} SCL=GP{} -> {}".format(
                bus_id,
                sda_pin,
                scl_pin,
                ["0x{:02X}".format(d) for d in devices],
            ))
            if config.I2C_LORA_ADDR in devices:
                try:
                    status = try_status(i2c)
                    found = (i2c, bus_id, sda_pin, scl_pin, status)
                    break
                except Exception as status_error:
                    print("Candidate I2C{} GP{}/GP{} rejected by status read: {}".format(
                        bus_id, sda_pin, scl_pin, status_error))
        except Exception as e:
            print("Scan I2C{} SDA=GP{} SCL=GP{} failed: {}".format(bus_id, sda_pin, scl_pin, e))

    if found is None:
        print("Bridge not found on any common RP2040 I2C mapping")
        print("Check common ground, SDA/SCL swap, and voltage-level compatibility")
        return

    i2c, bus_id, sda_pin, scl_pin, first_status = found
    print("Using I2C{} SDA=GP{} SCL=GP{}".format(bus_id, sda_pin, scl_pin))
    print(
        "Initial status: version={} queue={} tx_busy={} dropped={} error={}".format(
            first_status[1],
            first_status[2],
            first_status[3],
            first_status[4],
            first_status[5],
        )
    )

    for idx in range(10):
        try:
            status = try_status(i2c)
            print(
                "Read {} status: version={} queue={} tx_busy={} dropped={} error={}".format(
                    idx,
                    status[1],
                    status[2],
                    status[3],
                    status[4],
                    status[5],
                )
            )
        except Exception as e:
            print("Read {} failed: {}".format(idx, e))

        time.sleep_ms(500)


main()