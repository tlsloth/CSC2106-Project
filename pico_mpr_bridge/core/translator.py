# core/translator.py — Packet translation between protocols (LoRa<->MQTT, BLE<->MQTT)

import json
import config
from core import packet
from utils import logger
import struct
import ubinascii


TAG = "XLAT"


def translate_to_mqtt(pkt):
    """Translate an ingress packet (from LoRa or BLE) into an MQTT-publishable message.

    Returns (topic, json_payload_str) or None on failure.
    """
    src = pkt.get("src", "unknown")
    payload = pkt.get("payload", {})
    priority = pkt.get("priority", packet.PRIORITY_NORMAL)

    # Determine topic based on priority
    if priority <= packet.PRIORITY_HIGH:
        topic = config.MQTT_ALERT_TOPIC.format(node_id=src)
    else:
        topic = config.MQTT_DATA_TOPIC.format(node_id=src)

    # Build the MQTT message with metadata
    mqtt_msg = {
        "src": src,
        "dst": pkt.get("dst", "dashboard"),
        "seq": pkt.get("seq", 0),
        "priority": priority,
        "data": payload,
    }

    publishes = []
    try:
        standard_payload = json.dumps(mqtt_msg)
        if getattr(config, "UART_BRIDGE_COMPAT_KEEP_STANDARD", True):
            publishes.append((topic, standard_payload, False))

        if getattr(config, "ENABLE_UART_BRIDGE_COMPAT", False):
            compat = _build_uart_bridge_compat_publish(pkt)
            if compat:
                publishes.extend(compat)

        if not publishes:
            publishes.append((topic, standard_payload, False))

        # Backward compatible return for existing callers.
        if len(publishes) == 1:
            one = publishes[0]
            return (one[0], one[1], one[2])
        return publishes
    except Exception as e:
        logger.error(TAG, "Failed to build MQTT message: {}".format(e))
        return None


def _build_uart_bridge_compat_publish(pkt):
    """Build legacy UART bridge MQTT publishes used by existing dashboards.

    Returns list of publish tuples: (topic, payload_str, retain_bool)
    or [] if packet does not match DHT-like telemetry.
    """
    src = str(pkt.get("src", "unknown"))
    payload = pkt.get("payload", {})

    if not isinstance(payload, dict):
        return []

    temp = payload.get("T")
    if temp is None:
        temp = payload.get("temp", payload.get("temperature"))

    hum = payload.get("H")
    if hum is None:
        hum = payload.get("humidity", payload.get("hum"))

    # Only apply legacy shape for temperature/humidity sensor telemetry.
    if temp is None and hum is None:
        return []

    legacy = {
        "node": src,
        "T": temp,
        "H": hum,
        "rssi": pkt.get("rssi", payload.get("rssi", 0)),
    }

    legacy_payload = json.dumps(legacy)
    topic_data = config.MQTT_DATA_TOPIC.format(node_id=src)
    topic_latest = config.MQTT_TOPIC_LATEST.format(node_id=src)

    return [
        (topic_data, legacy_payload, False),
        (topic_latest, legacy_payload, True),
    ]


def translate_from_mqtt(topic, msg_bytes):
    """Translate an MQTT message into a packet for forwarding via LoRa or BLE.

    Used for commands going back from dashboard to sensor nodes.
    Returns a packet dict or None.
    """
    try:
        if isinstance(msg_bytes, (bytes, bytearray)):
            msg_bytes = msg_bytes.decode("utf-8")
        msg = json.loads(msg_bytes)

        dst = msg.get("dst", "unknown")
        payload = msg.get("data", msg.get("payload", {}))
        priority = msg.get("priority", packet.PRIORITY_NORMAL)

        pkt = packet.create_packet(
            src=config.NODE_ID,
            dst=dst,
            payload=payload,
            priority=priority,
        )
        return pkt
    except (ValueError, KeyError) as e:
        logger.error(TAG, "Failed to parse MQTT command: {}".format(e))
        return None


def _parse_csv_sensor(text):
    """Parse 'K:V,K:V' sensor strings (e.g. 'T:25.3,H:60.5') into a dict.

    Normalises common short keys used by Arduino DHT sketches:
      T  -> temp
      H  -> humidity
    Returns an empty dict on any parse failure.
    """
    _key_map = {"T": "temp", "H": "humidity"}
    parts = {}
    try:
        for item in text.split(","):
            k, v = item.split(":", 1)
            k = _key_map.get(k.strip(), k.strip())
            parts[k] = float(v.strip())
    except (ValueError, AttributeError):
        return {}
    return parts


def translate_lora_payload(raw_data, source_id="unknown"):
    """Parse a raw LoRa payload into a standard packet.

    Handles, in order:
      1. JSON-encoded payload dict
      2. CSV sensor format: 'T:25.3,H:60.5'  (Arduino DHT sensor)
      3. Raw string fallback
    """
    # Decode bytes once for subsequent steps
    if isinstance(raw_data, (bytes, bytearray)):
        try:
            raw_data = raw_data.decode("utf-8")
        except UnicodeError:
            raw_data = str(raw_data)

    # 1. Try JSON-encoded payload dict
    try:
        payload = json.loads(raw_data)
    except ValueError:
        # 2. Try Arduino-style CSV sensor format (e.g. "T:25.3,H:60.5")
        payload = _parse_csv_sensor(raw_data)
        if not payload:
            # 3. Last resort: preserve raw string
            payload = {"raw": raw_data}

    priority = packet.classify_priority(payload)
    return packet.create_packet(
        src=source_id,
        dst="dashboard",
        hop_dst=config.NODE_ID,
        payload=payload,
        priority=priority,
    )


def translate_ble_payload(raw_bytes, source_id="unknown"):
    """Parse raw BLE characteristic data into a standard packet.

    Expects either JSON bytes or a simple numeric value (e.g., distance in cm).
    """
    try:
        text = raw_bytes.decode("utf-8") if isinstance(raw_bytes, (bytes, bytearray)) else str(raw_bytes)

        # Try JSON
        try:
            payload = json.loads(text)
        except ValueError:
            # Try as numeric distance value
            try:
                val = float(text)
                payload = {"distance": val}
            except ValueError:
                payload = {"raw": text}

        priority = packet.classify_priority(payload)
        return packet.create_packet(
            src=source_id,
            dst="dashboard",
            hop_dst=config.NODE_ID,
            payload=payload,
            priority=priority,
        )
    except Exception as e:
        logger.error(TAG, "BLE payload translation error: {}".format(e))
        return None



import struct
import ubinascii

def decode_lora_hex(hex_str):
    """Unpacks Hex strings from the Uno back into standard Mesh JSON dicts."""
    try:
        b = ubinascii.unhexlify(hex_str.strip())
        ptype = b[0]
        
        # 0x00: JOIN REQ (<B16s16sIB)
        if ptype == 0x00 and len(b) == 38:
            u = struct.unpack('<B16s16sIB', b)
            return {
                "kind": "control", "type": "join_req",
                "node_id": u[1].decode('utf-8').strip('\x00'),
                "network": u[2].decode('utf-8').strip('\x00'),
                "auth": f"{u[3]:08x}", "seq": u[4]
            }
            
        # 0x02: HELLO (<B16s16s8sB)
        elif ptype == 0x02 and len(b) == 42:
            u = struct.unpack('<B16s16s8sB', b)
            return {
                "kind": "control", "type": "hello",
                "node_id": u[1].decode('utf-8').strip('\x00'),
                "network": u[2].decode('utf-8').strip('\x00'),
                "token": ubinascii.hexlify(u[3]).decode('utf-8'),
                "seq": u[4]
            }
            
        # 0x04: TELEMETRY (<B16s16s16s8shH)
        elif ptype == 0x04 and len(b) == 61:
            u = struct.unpack('<B16s16s16s8shH', b)
            return {
                "kind": "data", "type": "sensor_data",
                "node_id": u[1].decode('utf-8').strip('\x00'),
                "hop_dst": u[2].decode('utf-8').strip('\x00'),
                "dst": u[3].decode('utf-8').strip('\x00'),
                "token": ubinascii.hexlify(u[4]).decode('utf-8'),
                "payload": {"temp": u[5]/10.0, "hum": u[6]/10.0}
            }

        # 0x05: ROUTE QUERY (<B16s16s)
        elif ptype == 0x05 and len(b) == 33:
            u = struct.unpack('<B16s16s', b)
            return {
                "kind": "control", "type": "route_query",
                "src": u[1].decode('utf-8').strip('\x00'),
                "dst": u[2].decode('utf-8').strip('\x00')
            }

        # 0x06: ROUTE RESP (<B16s16s16s12sHB)
        elif ptype == 0x06 and len(b) == 64:
            u = struct.unpack('<B16s16s16s12sHB', b)
            return {
                "kind": "control", "type": "route_resp",
                "req_src": u[1].decode('utf-8').strip('\x00'),
                "dst": u[2].decode('utf-8').strip('\x00'),
                "next_hop": u[3].decode('utf-8').strip('\x00'),
                "via_protocol": u[4].decode('utf-8').strip('\x00'),
                "cost": u[5],
                "status": "ok" if u[6] == 1 else "no_route"
            }

    except Exception as e:
        print("Hex Decode Error:", e) # Don't silently swallow errors!
    return None

def encode_lora_hex(pkt):
    """Packs JSON Mesh dicts into binary Hex Strings for the Uno to transmit."""
    ptype = pkt.get("type")
    try:
        # 0x01: JOIN ACK (<B16sB16s8s)
        if ptype == "join_ack":
            tid = pkt.get("target_id", "").encode('utf-8')[:15]
            acc = 1 if pkt.get("accepted") else 0
            bid = pkt.get("bridge_id", "").encode('utf-8')[:15]
            tok_hex = pkt.get("token", "")
            tok_bytes = ubinascii.unhexlify(tok_hex) if tok_hex else b'\x00'*8
            b = struct.pack('<B16sB16s8s', 0x01, tid, acc, bid, tok_bytes)
            return ubinascii.hexlify(b).decode('utf-8')
            
        # 0x03: HELLO ACK (<B16s16s)
        elif ptype == "hello_ack":
            tid = pkt.get("target_id", "").encode('utf-8')[:15]
            bid = pkt.get("bridge_id", "").encode('utf-8')[:15]
            b = struct.pack('<B16s16s', 0x03, tid, bid)
            return ubinascii.hexlify(b).decode('utf-8')

        # 0x05: ROUTE QUERY (<B16s16s)
        elif ptype == "route_query":
            src = pkt.get("src", "").encode('utf-8')[:15]
            dst = pkt.get("dst", "").encode('utf-8')[:15]
            b = struct.pack('<B16s16s', 0x05, src, dst)
            return ubinascii.hexlify(b).decode('utf-8')

        # 0x06: ROUTE RESP (<B16s16s16s12sHB)
        elif ptype == "route_resp":
            req_src = pkt.get("req_src", "").encode('utf-8')[:15]
            dst = pkt.get("dst", "").encode('utf-8')[:15]
            nhop = pkt.get("next_hop", "").encode('utf-8')[:15]
            
            proto = pkt.get("via_protocol", "")
            proto_bytes = proto.encode('utf-8')[:11] if proto else b''
            
            cost = int(pkt.get("cost", 999))
            status = 1 if pkt.get("status") == "ok" else 0
            
            b = struct.pack('<B16s16s16s12sHB', 0x06, req_src, dst, nhop, proto_bytes, cost, status)
            return ubinascii.hexlify(b).decode('utf-8')

    except Exception as e:
        print("Hex Encode Error:", e)
    return None