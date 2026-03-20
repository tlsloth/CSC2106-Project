import json

import config
from core import packet
from core.neighbour import create_hello_payload, parse_hello
from utils import logger

TAG = "LoRaUART"

_uart = None


def _extract_source_id(payload_bytes):
    try:
        if isinstance(payload_bytes, (bytes, bytearray)):
            payload_text = payload_bytes.decode("utf-8")
        else:
            payload_text = str(payload_bytes)
        msg = json.loads(payload_text)
        return msg.get("src") or msg.get("node_id") or "lora_uart"
    except Exception:
        return "lora_uart"


def _parse_line(raw_line):
    line = raw_line.strip()
    if not line:
        return None

    parts = line.split("|", 3)
    kind = parts[0]

    if kind == "LORA_RX" and len(parts) >= 4:
        try:
            rssi = int(float(parts[1]))
        except Exception:
            rssi = 0

        try:
            snr = float(parts[2])
        except Exception:
            snr = 0.0

        payload = parts[3].encode("utf-8")
        return {"type": "LORA_RX", "rssi": rssi, "snr": snr, "payload": payload}

    if kind == "LORA_STATUS":
        return {"type": "LORA_STATUS", "line": line}

    if kind == "LORA_ERR":
        return {"type": "LORA_ERR", "line": line}

    return {"type": "UNKNOWN", "line": line}


def _send_line(text):
    if _uart is None:
        raise OSError("UART bridge not initialised")
    _uart.write((text + "\n").encode("utf-8"))


def init():
    global _uart
    try:
        from machine import Pin, UART

        _uart = UART(
            config.UART_LORA_ID,
            baudrate=config.UART_LORA_BAUD,
            tx=Pin(config.UART_LORA_TX_PIN),
            rx=Pin(config.UART_LORA_RX_PIN),
            timeout=config.UART_LORA_TIMEOUT_MS,
        )

        logger.info(
            TAG,
            "UART bridge ready: UART{} TX=GP{} RX=GP{} @ {}".format(
                config.UART_LORA_ID,
                config.UART_LORA_TX_PIN,
                config.UART_LORA_RX_PIN,
                config.UART_LORA_BAUD,
            ),
        )
        return True
    except Exception as e:
        logger.error(TAG, "UART init failed: {}".format(e))
        _uart = None
        return False


def is_available():
    return _uart is not None


async def rx_task(ingress_queue, neighbour_table):
    import uasyncio as asyncio
    from core.translator import translate_lora_payload

    logger.info(TAG, "LoRa-over-UART RX task started")

    while True:
        try:
            if _uart is None:
                await asyncio.sleep(5)
                continue

            if _uart.any():
                raw = _uart.readline()
                if not raw:
                    await asyncio.sleep_ms(20)
                    continue

                try:
                    text = raw.decode("utf-8", "ignore")
                except Exception:
                    text = str(raw)

                parsed = _parse_line(text)
                if not parsed:
                    await asyncio.sleep_ms(20)
                    continue

                if parsed["type"] == "LORA_STATUS":
                    logger.debug(TAG, "Bridge status: {}".format(parsed["line"]))
                elif parsed["type"] == "LORA_ERR":
                    logger.warn(TAG, "Bridge error: {}".format(parsed["line"]))
                elif parsed["type"] == "LORA_RX":
                    payload = parsed["payload"]
                    hello = parse_hello(payload)
                    if hello:
                        neighbour_table.update(
                            hello["node_id"],
                            protocols=["LoRa"],
                            rssi=parsed["rssi"],
                            capabilities=hello.get("capabilities", ["LoRa"]),
                        )
                    else:
                        pkt = translate_lora_payload(
                            payload,
                            source_id=_extract_source_id(payload),
                        )
                        if pkt:
                            ingress_queue.push(
                                pkt.get("priority", packet.PRIORITY_NORMAL),
                                pkt,
                            )
                else:
                    logger.debug(TAG, "Unrecognized bridge line: {}".format(parsed["line"]))

        except Exception as e:
            logger.error(TAG, "RX error: {}".format(e))

        await asyncio.sleep_ms(20)


async def tx_task(egress_queue):
    import uasyncio as asyncio

    logger.info(TAG, "LoRa-over-UART TX task started")
    while True:
        try:
            if _uart is not None and not egress_queue.is_empty():
                pkt = egress_queue.pop()
                if pkt:
                    payload = packet.encode_packet(pkt)
                    text_payload = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else str(payload)
                    _send_line("LORA_TX|{}".format(text_payload))
                    logger.debug(TAG, "TX forwarded to bridge: {} bytes".format(len(text_payload)))
        except Exception as e:
            logger.error(TAG, "TX error: {}".format(e))

        await asyncio.sleep_ms(200)


async def hello_task(neighbour_table):
    import uasyncio as asyncio

    logger.info(TAG, "LoRa-over-UART Hello task started")
    while True:
        try:
            if _uart is not None:
                hello = create_hello_payload()
                _send_line("LORA_TX|{}".format(json.dumps(hello)))
                logger.debug(TAG, "Sent LoRa Hello via UART bridge")
        except Exception as e:
            logger.error(TAG, "Hello broadcast error: {}".format(e))

        await asyncio.sleep(config.HELLO_INTERVAL)
