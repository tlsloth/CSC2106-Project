try:
    import ujson as json
except ImportError:
    import json

import config
from core.neighbour import create_hello_payload
from core.security import check_join_auth, check_node_token, generate_join_token, xor_token_hex
from utils import logger
import uasyncio as asyncio

TAG = "LoRaUART"
DEFAULT_PRIORITY = 5

_uart = None
_rx_buf = b""
_node_tokens = {}
radio_lock = asyncio.Lock()
tx_done_event = asyncio.Event()


def _json_dumps(obj):
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


def _message_kind(msg):
    kind = str(msg.get("kind") or "").strip().lower()
    if kind in ("control", "data"):
        return kind

    msg_type = str(msg.get("type") or "").strip().lower()
    if msg_type in ("join_req", "join_ack", "hello", "hello_ack", "route_query", "route_resp"):
        return "control"
    return "data"


def _send_join_ack(req_msg, accepted, egress_queue, reason="ok", token=""):
    node_id = str(req_msg.get("node_id") or req_msg.get("src") or "unknown")
    logger.info(
        TAG,
        "TX join_ack to {} accepted={} reason={} via UART bridge command".format(
            node_id,
            bool(accepted),
            reason,
        ),
    )
    encrypted_token = xor_token_hex(token, getattr(config, "MESH_JOIN_KEY", "")) if token else ""
    # create json packet here, then pass to bridge to do TX only
    ack = {
        "kind": "control",
        "type": "join_ack",
        "accepted": bool(accepted),
        "src": config.NODE_ID,
        "bridge_id": config.NODE_ID,
        "dst": node_id,
        "reason": reason,
        "token": encrypted_token,
    }
    egress_queue.push(DEFAULT_PRIORITY, ack)


def _send_route_response(query_msg, route, egress_queue):
    response = {
        "kind": "control",
        "type": "route_resp",
        "node_id": config.NODE_ID,
        "src": config.NODE_ID,
        "req_src": query_msg.get("src") or query_msg.get("node_id", "unknown"),
        "dst": query_msg.get("dst", "unknown"),
        "status": "ok" if route else "no_route",
        "next_hop": route["next_hop"] if route else None,
        "via_protocol": route["via_protocol"] if route else None,
        "cost": route["cost"] if route else None,
        "seq": query_msg.get("seq", 0),
    }
    egress_queue.push(DEFAULT_PRIORITY, response)


def _send_hello_ack(msg, egress_queue):
    node_id = str(msg.get("node_id") or msg.get("src") or "unknown")
    logger.debug(TAG, f"Queueing hello_ack to target_id: {node_id}")

    ack = {
        "kind": "control",
        "type": "hello_ack",
        "target_id": node_id,
        "bridge_id": config.NODE_ID
    }

    egress_queue.push(DEFAULT_PRIORITY, ack)


def _parse_line(raw_line):
    line = raw_line.strip()
    if not line:
        return None

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


async def rx_task(ingress_queue, egress_queue, neighbour_table, routing_table=None):
    from core.translator import translate_lora_payload
    global _rx_buf, _uart

    logger.info(TAG, "LoRa-over-UART RX task started")

    while True:
        try:
            if _uart is None:
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

                    while b"\n" in _rx_buf:
                        line, _rx_buf = _rx_buf.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue

                        try:
                            text = line.decode("utf-8", "ignore")
                        except Exception:
                            text = str(line)

                        logger.info(TAG, "Received through UART: {}".format(text))
                        if "LORA_STATUS|TX_DONE" in text:
                            tx_done_event.set()
                            continue
                        

                        parsed = _parse_line(text)
                        if not parsed:
                            continue

                        if parsed["type"] == "LORA_STATUS":
                            logger.debug(TAG, "Bridge status: {}".format(parsed["line"]))
                        elif parsed["type"] == "LORA_ERR":
                            logger.warn(TAG, "Bridge error: {}".format(parsed["line"]))
                        elif parsed["type"] == "LORA_RX":
                            payload = parsed["payload"]
                            source_id = parsed.get("node") or _extract_source_id(payload)

                            msg = _parse_json_message(payload)
                            if not isinstance(msg, dict):
                                logger.warn(TAG, "Dropped non-JSON payload from {}".format(source_id))
                                continue

                            msg_type = str(msg.get("type") or "")
                            if not msg_type:
                                logger.warn(TAG, "Dropped packet from {} with no type".format(source_id))
                                continue

                            msg_kind = _message_kind(msg)
                            msg_node_id = str(msg.get("node_id") or msg.get("src") or source_id)

                            if msg_type == "join_req" and msg_kind == "control":
                                ok, reason = check_join_auth(
                                    msg,
                                    getattr(config, "MESH_NETWORK_NAME", ""),
                                    getattr(config, "MESH_JOIN_KEY", ""),
                                )
                                node_id = str(msg.get("node_id") or source_id)
                                token = ""
                                if ok:
                                    token = generate_join_token(
                                        token_bytes=int(getattr(config, "MESH_JOIN_TOKEN_BYTES", 8) or 8),
                                        entropy_hint=len(_node_tokens),
                                    )
                                    _node_tokens[node_id] = token
                                    neighbour_table.update(
                                        node_id,
                                        protocols=["LoRa"],
                                        rssi=parsed.get("rssi", 0),
                                        capabilities=msg.get("capabilities", ["LoRa"]),
                                    )
                                    logger.info(TAG, "Join accepted for {}".format(node_id))
                                else:
                                    _node_tokens.pop(node_id, None)
                                    logger.warn(TAG, "Join rejected for {} ({})".format(source_id, reason))
                                _send_join_ack(msg, ok, egress_queue, reason, token)
                                continue


                            token_ok, token_reason = check_node_token(
                                msg,
                                _node_tokens.get(msg_node_id),
                                getattr(config, "MESH_JOIN_KEY", ""),
                            )
                            if not token_ok:
                                logger.warn(
                                    TAG,
                                    "Dropped {} from {} ({})".format(
                                        msg_type,
                                        msg_node_id,
                                        token_reason,
                                    ),
                                )
                                continue

                            neighbour_table.update(
                                msg_node_id,
                                protocols=["LoRa"],
                                rssi=parsed.get("rssi", 0),
                                capabilities=msg.get("capabilities", ["LoRa"]),
                            )
                            if msg_kind == "control":
                                if msg_type == "route_query":
                                    route = None
                                    dst = msg.get("dst")
                                    if dst == config.NODE_ID:
                                        route = {
                                            "next_hop": config.NODE_ID,
                                            "via_protocol": "LOCAL",
                                            "cost": 0,
                                        }
                                    elif routing_table is not None and dst:
                                        route = routing_table.lookup(dst)
                                    logger.info(TAG, "route_query dst={} local_id={} route={}".format(dst, config.NODE_ID, route))
                                    _send_route_response(msg, route,egress_queue)
                                    continue
                                
                                if msg_type == "hello":
                                    _send_hello_ack(msg,egress_queue)
                                if msg_type in ("hello", "hello_ack", "join_ack"):
                                    continue

                                if isinstance(msg, dict):
                                    msg["rssi"] = parsed.get("rssi",0)
                                    ingress_queue.push(
                                        msg.get("priority", DEFAULT_PRIORITY),
                                        msg
                                    )
                                    logger.debug(TAG, f"Pushed {msg_type} to ingress queue")
                                else:
                                    logger.warn(TAG, f"Dropped unformatted message: {msg} ")

                        else:
                            logger.debug(TAG, "Unrecognized bridge line: {}".format(parsed["line"]))

        except Exception as e:
            logger.error(TAG, "RX error: {}".format(e))
            try:
                if _uart is not None:
                    _uart.deinit()
            except Exception:
                pass
            _uart = None

        await asyncio.sleep_ms(20)

# Used to pass from pico to another lora device
async def tx_task(egress_queue):
    logger.info(TAG, "LoRa-over-UART TX task started")
    while True:
        try:
            if _uart is not None and not egress_queue.is_empty():
                pkt = egress_queue.pop()
                if pkt:
                    if isinstance(pkt, dict):
                        if "kind" not in pkt:
                            pkt["kind"] = "data"
                        text_payload = _json_dumps(pkt)
                    elif isinstance(pkt, (bytes, bytearray)):
                        text_payload = pkt.decode("utf-8", "ignore")
                    else:
                        text_payload = str(pkt)
                    async with radio_lock:
                        tx_done_event.clear()
                        _send_line(f"LORA_TX|{text_payload}")
                        logger.debug(TAG, f"Commanded Uno to TX:{len(text_payload)} bytes")
                        try:
                            await asyncio.wait_for(tx_done_event.wait(),timeout=5.0)
                            logger.debug(TAG,"Uno confirmed TX completion")
                        except asyncio.TimeoutError:
                            logger.warn(TAG, "Timeout waiting for Uno TX_DONE confirmation. Releasing lock...")
        except Exception as e:
            logger.error(TAG, "TX error: {}".format(e))

        await asyncio.sleep_ms(50)

# Proof that bridge is still alive to neighbouring bridges
async def hello_task(neighbour_table, egress_queue):
    import uasyncio as asyncio

    logger.info(TAG, "LoRa-over-UART Hello task started")
    while True:
        try:
            if _uart is not None:
                hello = create_hello_payload()
                if isinstance(hello, dict) and "kind" not in hello:
                    hello["kind"] = "control"
                egress_queue.push(DEFAULT_PRIORITY+1, hello)
                logger.debug(TAG, "Sent LoRa Hello via UART bridge")
        except Exception as e:
            logger.error(TAG, "Hello broadcast error: {}".format(e))

        await asyncio.sleep(getattr(config, "HELLO_INTERVAL", 30))