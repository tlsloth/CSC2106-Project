# core/packet.py — Common packet metadata helpers and fragmentation

import time
import config
from utils import logger

TAG = "PKT"

# Priority levels
PRIORITY_CRITICAL = 0
PRIORITY_HIGH     = 1
PRIORITY_NORMAL   = 5
PRIORITY_LOW      = 10

# Global sequence counter
_seq_counter = 0


def _next_seq():
    global _seq_counter
    _seq_counter = (_seq_counter + 1) % 65536
    return _seq_counter


def create_packet(src, dst, payload, priority=PRIORITY_NORMAL, hop_src=None, hop_dst=None):
    """Create a new packet dictionary with the standard header."""
    if hop_src is None:
        hop_src = config.NODE_ID
    if hop_dst is None:
        hop_dst = dst

    return {
        "src": src,
        "dst": dst,
        "hop_src": hop_src,
        "hop_dst": hop_dst,
        "ttl": config.PACKET_TTL,
        "priority": priority,
        "seq": _next_seq(),
        "frag": {"index": 0, "total": 1},
        "payload": payload,
    }


def fragment_payload(payload_bytes, max_size=None):
    """Fragment a large payload into chunks. Returns list of (index, total, chunk)."""
    if max_size is None:
        max_size = config.MAX_PAYLOAD_SIZE
    if len(payload_bytes) <= max_size:
        return [(0, 1, payload_bytes)]

    fragments = []
    total = (len(payload_bytes) + max_size - 1) // max_size
    for i in range(total):
        start = i * max_size
        end = min(start + max_size, len(payload_bytes))
        fragments.append((i, total, payload_bytes[start:end]))
    logger.debug(TAG, "Fragmented payload into {} parts".format(total))
    return fragments


def is_expired(packet):
    """Check if a packet's TTL has reached zero."""
    return packet.get("ttl", 0) <= 0


def decrement_ttl(packet):
    """Decrement TTL and return the packet. Returns None if expired."""
    packet["ttl"] = packet.get("ttl", 0) - 1
    if packet["ttl"] <= 0:
        logger.warn(TAG, "Packet from {} seq={} expired (TTL=0)".format(
            packet.get("src", "?"), packet.get("seq", "?")))
        return None
    return packet


def classify_priority(payload):
    """Classify incoming sensor data into a priority level."""
    if isinstance(payload, dict):
        # Temperature alert
        temp = payload.get("temp") or payload.get("temperature")
        if temp is not None:
            try:
                if float(temp) >= config.TEMP_ALERT_THRESHOLD:
                    return PRIORITY_HIGH
            except (ValueError, TypeError):
                pass

        # Distance alert (ultrasonic intrusion)
        dist = payload.get("dist") or payload.get("distance")
        if dist is not None:
            try:
                if float(dist) <= config.DISTANCE_ALERT_MIN:
                    return PRIORITY_HIGH
            except (ValueError, TypeError):
                pass

        # Check for explicit alert/sos flags
        if payload.get("sos") or payload.get("alert") == "critical":
            return PRIORITY_CRITICAL

    return PRIORITY_NORMAL
