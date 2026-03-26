# core/translator.py — Packet translation between protocols (LoRa<->MQTT, BLE<->MQTT)

import json
import config
from core import packet
from utils import logger

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
      1. Full JSON packet (from another bridge node)
      2. JSON-encoded payload dict
      3. CSV sensor format: 'T:25.3,H:60.5'  (Arduino DHT sensor)
      4. Raw string fallback
    """
    # 1. Try full JSON packet first
    pkt = packet.decode_packet(raw_data)
    if pkt:
        return pkt

    # Decode bytes once for subsequent steps
    if isinstance(raw_data, (bytes, bytearray)):
        try:
            raw_data = raw_data.decode("utf-8")
        except UnicodeError:
            raw_data = str(raw_data)

    # 2. Try JSON-encoded payload dict
    try:
        payload = json.loads(raw_data)
    except ValueError:
        # 3. Try Arduino-style CSV sensor format (e.g. "T:25.3,H:60.5")
        payload = _parse_csv_sensor(raw_data)
        if not payload:
            # 4. Last resort: preserve raw string
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
