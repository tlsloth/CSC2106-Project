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

    try:
        return (topic, json.dumps(mqtt_msg))
    except Exception as e:
        logger.error(TAG, "Failed to build MQTT message: {}".format(e))
        return None


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


def translate_lora_payload(raw_data, source_id="unknown"):
    """Parse a raw LoRa payload into a standard packet.

    Supports both JSON and simple struct-packed formats.
    """
    # Try JSON first
    pkt = packet.decode_packet(raw_data)
    if pkt:
        return pkt

    # Fallback: treat as raw sensor payload (e.g., temperature float as text)
    try:
        if isinstance(raw_data, (bytes, bytearray)):
            raw_data = raw_data.decode("utf-8")

        # Try to parse as simple key=value or JSON snippet
        payload = json.loads(raw_data)
    except (ValueError, UnicodeError):
        # Last resort: treat entire thing as a raw value
        payload = {"raw": str(raw_data)}

    priority = packet.classify_priority(payload)
    return packet.create_packet(
        src=source_id,
        dst="dashboard",
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
            payload=payload,
            priority=priority,
        )
    except Exception as e:
        logger.error(TAG, "BLE payload translation error: {}".format(e))
        return None
