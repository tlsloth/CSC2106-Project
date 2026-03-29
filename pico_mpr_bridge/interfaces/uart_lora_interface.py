try:
    import ujson as json
except ImportError:
    import json

import config
from core import packet
from core.neighbour import create_hello_payload, parse_hello
from utils import logger

TAG = "LoRaUART"

_uart = None
_rx_buf = b""


def _json_dumps(obj):
    # Keep UART lines compact; fallback for runtimes that don't support separators.
    try:
        return json.dumps(obj, separators=(",", ":"))
    except TypeError:
        return json.dumps(obj)


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


def _parse_json_message(payload_bytes):
    try:
        if isinstance(payload_bytes, (bytes, bytearray)):
            payload_text = payload_bytes.decode("utf-8")
        else:
            payload_text = str(payload_bytes)
        return json.loads(payload_text)
    except Exception:
        return None


def _check_join_auth(msg):
    network = msg.get("network") or msg.get("network_name")
    if network != getattr(config, "MESH_NETWORK_NAME", ""):
        return False, "network_mismatch"

    expected_key = getattr(config, "MESH_JOIN_KEY", "")
    provided_key = msg.get("auth") or msg.get("key") or ""
    if expected_key and provided_key != expected_key:
        return False, "auth_failed"

    return True, "ok"


def _send_join_ack(req_msg, accepted, reason="ok"):
    node_id = str(req_msg.get("node_id") or req_msg.get("src") or "unknown")
    logger.info(
        TAG,
        "TX join_ack to {} accepted={} reason={} via UART bridge command".format(
            node_id,
            bool(accepted),
            reason,
        ),
    )
    _send_line(
        "LORA_JOIN_ACK|{}|{}|{}".format(
            1 if accepted else 0,
            config.NODE_ID,
            node_id,
        )
    )


def _send_route_response(query_msg, route):
    response = {
        "type": "route_resp",
        "node_id": config.NODE_ID,
        "req_src": query_msg.get("src") or query_msg.get("node_id", "unknown"),
        "dst": query_msg.get("dst", "unknown"),
        "status": "ok" if route else "no_route",
        "next_hop": route["next_hop"] if route else None,
        "via_protocol": route["via_protocol"] if route else None,
        "cost": route["cost"] if route else None,
        "seq": query_msg.get("seq", 0),
    }
    _send_line("LORA_TX|{}".format(_json_dumps(response)))


def _parse_line(raw_line):
    line = raw_line.strip()
    if not line:
        return None

    # JSON format: {"raw": "T:25.3,H:60.5", "node": "A", "rssi": -75}
    # This is the format produced by lora_uart_bridge.py-compatible Arduino firmware.
    if line.startswith('{'):
        try:
            obj = json.loads(line)
            raw = obj.get("raw", "")
            node = str(obj.get("node", "lora_uart"))
            rssi = int(float(obj.get("rssi", 0)))
            return {
                "type": "LORA_RX",
                "rssi": rssi,
                "snr": 0.0,
                "payload": raw.encode("utf-8"),
                "node": node,
            }
        except (ValueError, KeyError, TypeError):
            pass

    # Pipe-delimited formats from UNO bridge:
    # - LORA_RX|rssi|snr|payload
    # - LORA_RX|rssi|snr|node|payload
    parts = line.split("|", 4)
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

        if len(parts) >= 5:
            node = parts[3].strip() or "lora_uart"
            payload = parts[4].encode("utf-8")
            return {
                "type": "LORA_RX",
                "rssi": rssi,
                "snr": snr,
                "payload": payload,
                "node": node,
            }

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
    out = (text + "\n").encode("utf-8")
    written = _uart.write(out)
    if written is None:
        logger.warn(TAG, "UART write returned None")
    elif written != len(out):
        logger.warn(TAG, "UART partial write: {}/{} bytes".format(written, len(out)))


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


async def rx_task(ingress_queue, neighbour_table, routing_table=None):
    import uasyncio as asyncio
    from core.translator import translate_lora_payload
    global _rx_buf, _uart

    logger.info(TAG, "LoRa-over-UART RX task started")

    while True:
        try:
            if _uart is None:
                # Allow recovery if UART init fails once at startup.
                if init():
                    logger.info(TAG, "UART recovered")
                else:
                    await asyncio.sleep(5)
                continue

            available = _uart.any()
            if available:
                chunk = _uart.read(available)
                if chunk:
                    _rx_buf += chunk

                    # Match lora_uart_bridge.py behavior: parse complete newline-delimited lines.
                    while b"\n" in _rx_buf:
                        line, _rx_buf = _rx_buf.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue

                        try:
                            text = line.decode("utf-8", "ignore")
                        except Exception:
                            text = str(line)

                        logger.info(TAG, "UART RX: {}".format(text))

                        parsed = _parse_line(text)
                        if not parsed:
                            continue

                        if parsed["type"] == "LORA_STATUS":
                            logger.debug(TAG, "Bridge status: {}".format(parsed["line"]))
                        elif parsed["type"] == "LORA_ERR":
                            logger.warn(TAG, "Bridge error: {}".format(parsed["line"]))
                        elif parsed["type"] == "LORA_RX":
                            payload = parsed["payload"]
                            # Use the explicit node field when present (JSON format), else
                            # fall back to extracting source_id from the payload body.
                            source_id = parsed.get("node") or _extract_source_id(payload)

                            # For simple endpoint nodes (e.g., DHT sender) that don't emit hello,
                            # treat inbound data as proof of neighbour presence.
                            if source_id and source_id != "lora_uart":
                                neighbour_table.update(
                                    source_id,
                                    protocols=["LoRa"],
                                    rssi=parsed.get("rssi", 0),
                                    capabilities=["LoRa"],
                                )

                            msg = _parse_json_message(payload)
                            if isinstance(msg, dict) and msg.get("type") == "join_req":
                                ok, reason = _check_join_auth(msg)
                                if ok:
                                    node_id = str(msg.get("node_id") or source_id)
                                    neighbour_table.update(
                                        node_id,
                                        protocols=["LoRa"],
                                        rssi=parsed.get("rssi", 0),
                                        capabilities=msg.get("capabilities", ["LoRa"]),
                                    )
                                    logger.info(TAG, "Join accepted for {}".format(node_id))
                                else:
                                    logger.warn(TAG, "Join rejected for {} ({})".format(source_id, reason))
                                _send_join_ack(msg, ok, reason=reason)
                                continue

                            if isinstance(msg, dict) and msg.get("type") == "route_query":
                                route = None
                                if routing_table is not None:
                                    dst = msg.get("dst")
                                    if dst:
                                        route = routing_table.lookup(dst)
                                _send_route_response(msg, route)
                                continue

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
                                    source_id=source_id,
                                )
                                if pkt:
                                    pkt["rssi"] = parsed.get("rssi", 0)
                                    ingress_queue.push(
                                        pkt.get("priority", packet.PRIORITY_NORMAL),
                                        pkt,
                                    )
                        else:
                            logger.debug(TAG, "Unrecognized bridge line: {}".format(parsed["line"]))

        except Exception as e:
            logger.error(TAG, "RX error: {}".format(e))
            # Force a clean re-init path on next loop if UART gets wedged.
            try:
                if _uart is not None:
                    _uart.deinit()
            except Exception:
                pass
            _uart = None

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
