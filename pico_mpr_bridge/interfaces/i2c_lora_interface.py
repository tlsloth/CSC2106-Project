import json

import config
from utils import logger
from core import packet
from core.neighbour import create_hello_payload, parse_hello

TAG = "LoRaI2C"

REG_STATUS = 0x00
REG_RX_HEADER = 0x01
REG_RX_DATA = 0x02
REG_RX_POP = 0x03
REG_TX_BEGIN = 0x10
REG_TX_DATA = 0x11
REG_TX_COMMIT = 0x12

STATUS_MAGIC = 0xA5
STATUS_SIZE = 6
RX_HEADER_SIZE = 4
HELLO_FLAG = 0x01

_i2c = None


def _candidate_i2c_configs():
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

    candidates = [configured]
    for item in common:
        if item not in candidates:
            candidates.append(item)
    return candidates


def _line_state(Pin, pin_number):
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


def _make_i2c(I2C, Pin, bus_id, sda_pin, scl_pin):
    return I2C(
        bus_id,
        scl=Pin(scl_pin),
        sda=Pin(sda_pin),
        freq=config.I2C_LORA_FREQ,
    )


def _to_signed(value):
    return value - 256 if value > 127 else value


def _infer_source_id(raw_data):
    try:
        if isinstance(raw_data, (bytes, bytearray)):
            raw_data = raw_data.decode("utf-8")
        msg = json.loads(raw_data)
        return msg.get("src") or msg.get("node_id") or "lora_i2c"
    except Exception:
        return "lora_i2c"


def _write_register(register, payload=b""):
    if _i2c is None:
        raise OSError("I2C bridge not initialised")
    _i2c.writeto(config.I2C_LORA_ADDR, bytes([register]) + bytes(payload))


def _read_register(register, size, args=b""):
    if _i2c is None:
        raise OSError("I2C bridge not initialised")
    # Some MicroPython builds reject keyword args for writeto(stop=...).
    _i2c.writeto(config.I2C_LORA_ADDR, bytes([register]) + bytes(args), False)
    return _i2c.readfrom(config.I2C_LORA_ADDR, size)


def _read_status():
    data = _read_register(REG_STATUS, STATUS_SIZE)
    if len(data) != STATUS_SIZE or data[0] != STATUS_MAGIC:
        raise OSError("invalid bridge status response")
    return {
        "version": data[1],
        "queue_depth": data[2],
        "tx_busy": data[3],
        "dropped": data[4],
        "last_error": data[5],
    }


def _read_frame():
    header = _read_register(REG_RX_HEADER, RX_HEADER_SIZE)
    if len(header) != RX_HEADER_SIZE:
        raise OSError("short RX header")

    length = header[0]
    if length <= 0 or length > config.I2C_LORA_MAX_FRAME:
        return None

    payload = bytearray()
    offset = 0
    chunk_size = max(1, min(config.I2C_LORA_CHUNK, 24))

    while offset < length:
        take = min(chunk_size, length - offset)
        chunk = _read_register(REG_RX_DATA, take, bytes([offset]))
        if len(chunk) != take:
            raise OSError("short RX payload chunk")
        payload.extend(chunk)
        offset += take

    return {
        "data": bytes(payload),
        "rssi": _to_signed(header[1]),
        "snr": _to_signed(header[2]) / 4.0,
        "flags": header[3],
    }


def _tx_packet_bytes(data):
    if len(data) > config.I2C_LORA_MAX_FRAME:
        raise ValueError("payload too large for I2C bridge")

    _write_register(REG_TX_BEGIN, bytes([len(data)]))

    offset = 0
    chunk_size = max(1, min(config.I2C_LORA_CHUNK, 24))
    while offset < len(data):
        chunk = data[offset:offset + chunk_size]
        _write_register(REG_TX_DATA, bytes([offset]) + chunk)
        offset += len(chunk)

    _write_register(REG_TX_COMMIT)


def init():
    global _i2c
    try:
        from machine import I2C, Pin

        sda_up, sda_down, sda_state = _line_state(Pin, config.I2C_LORA_SDA_PIN)
        scl_up, scl_down, scl_state = _line_state(Pin, config.I2C_LORA_SCL_PIN)
        logger.debug(TAG, "Configured line state SDA GP{} up/down={}/{} {} | SCL GP{} up/down={}/{} {}".format(
            config.I2C_LORA_SDA_PIN,
            sda_up,
            sda_down,
            sda_state,
            config.I2C_LORA_SCL_PIN,
            scl_up,
            scl_down,
            scl_state,
        ))

        last_scan = None
        for bus_id, sda_pin, scl_pin in _candidate_i2c_configs():
            try:
                probe = _make_i2c(I2C, Pin, bus_id, sda_pin, scl_pin)
                devices = probe.scan()
                last_scan = (bus_id, sda_pin, scl_pin, devices)
                if config.I2C_LORA_ADDR not in devices:
                    continue

                # Accept the mapping only if the bridge returns a valid status frame.
                _i2c = probe
                status = _read_status()
                if (bus_id, sda_pin, scl_pin) != (
                    config.I2C_LORA_ID,
                    config.I2C_LORA_SDA_PIN,
                    config.I2C_LORA_SCL_PIN,
                ):
                    logger.warn(TAG, "Bridge found on fallback I2C{} SDA=GP{} SCL=GP{}".format(
                        bus_id, sda_pin, scl_pin))
                logger.info(TAG, "I2C bridge ready: addr=0x{:02X} version={} queue={}".format(
                    config.I2C_LORA_ADDR,
                    status["version"],
                    status["queue_depth"],
                ))
                return True
            except Exception:
                continue

        if last_scan:
            bus_id, sda_pin, scl_pin, devices = last_scan
            logger.error(TAG, "Bridge address 0x{:02X} not found. Last scan I2C{} SDA=GP{} SCL=GP{} saw {}".format(
                config.I2C_LORA_ADDR,
                bus_id,
                sda_pin,
                scl_pin,
                ["0x{:02X}".format(dev) for dev in devices],
            ))
        else:
            logger.error(TAG, "Bridge address 0x{:02X} not found on any common I2C mapping".format(
                config.I2C_LORA_ADDR))
        _i2c = None
        return False
    except Exception as e:
        logger.error(TAG, "I2C LoRa init failed: {}".format(e))
        _i2c = None
        return False


def is_available():
    return _i2c is not None


async def rx_task(ingress_queue, neighbour_table):
    import uasyncio as asyncio
    from core.translator import translate_lora_payload

    logger.info(TAG, "LoRa-over-I2C RX task started")

    while True:
        try:
            if _i2c is None:
                await asyncio.sleep(5)
                continue

            status = _read_status()
            if status["queue_depth"] > 0:
                frame = _read_frame()
                if frame:
                    hello = parse_hello(frame["data"])
                    if hello:
                        neighbour_table.update(
                            hello["node_id"],
                            protocols=["LoRa"],
                            rssi=frame["rssi"],
                            capabilities=hello.get("capabilities", ["LoRa"]),
                        )
                    else:
                        pkt = translate_lora_payload(
                            frame["data"],
                            source_id=_infer_source_id(frame["data"]),
                        )
                        if pkt:
                            ingress_queue.push(
                                pkt.get("priority", packet.PRIORITY_NORMAL),
                                pkt,
                            )

                _write_register(REG_RX_POP)

            await asyncio.sleep_ms(config.I2C_LORA_POLL_MS)
        except Exception as e:
            logger.error(TAG, "RX error: {}".format(e))
            await asyncio.sleep_ms(config.I2C_LORA_POLL_MS)


async def tx_task(egress_queue):
    import uasyncio as asyncio

    logger.info(TAG, "LoRa-over-I2C TX task started")

    while True:
        try:
            if _i2c is not None and not egress_queue.is_empty():
                pkt = egress_queue.pop()
                if pkt:
                    data = packet.encode_packet(pkt)
                    _tx_packet_bytes(data)
                    logger.debug(TAG, "TX: {} bytes to {}".format(len(data), pkt.get("dst", "?")))
        except Exception as e:
            logger.error(TAG, "TX error: {}".format(e))

        await asyncio.sleep_ms(200)


async def hello_task(neighbour_table):
    import uasyncio as asyncio

    logger.info(TAG, "LoRa-over-I2C Hello task started")

    while True:
        try:
            if _i2c is not None:
                hello = create_hello_payload()
                data = json.dumps(hello).encode("utf-8")
                _tx_packet_bytes(data)
                logger.debug(TAG, "Sent LoRa Hello broadcast via I2C bridge")
        except Exception as e:
            logger.error(TAG, "Hello broadcast error: {}".format(e))

        await asyncio.sleep(config.HELLO_INTERVAL)