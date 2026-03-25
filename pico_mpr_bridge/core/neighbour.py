# core/neighbour.py — Neighbour table management + Hello protocol

import time
import json
import config
from utils import logger
from core import packet

TAG = "NBOR"


class NeighbourTable:
    """Maintains discovered neighbours with protocol capabilities and freshness."""

    def __init__(self):
        # { node_id: {"protocols": [...], "rssi": int, "last_seen": int, "capabilities": [...]} }
        self._table = {}

    def update(self, node_id, protocols, rssi=0, capabilities=None):
        """Add or update a neighbour entry."""
        if node_id == config.NODE_ID:
            return  # Ignore self

        now = time.time()
        if node_id in self._table:
            entry = self._table[node_id]
            entry["protocols"] = list(set(entry["protocols"]) | set(protocols))
            entry["rssi"] = rssi
            entry["last_seen"] = now
            if capabilities:
                entry["capabilities"] = capabilities
        else:
            self._table[node_id] = {
                "protocols": list(protocols),
                "rssi": rssi,
                "last_seen": now,
                "capabilities": capabilities or list(protocols),
            }
            logger.info(TAG, "New neighbour: {} via {}".format(node_id, protocols))

    def remove(self, node_id):
        """Remove a neighbour from the table."""
        if node_id in self._table:
            del self._table[node_id]
            logger.info(TAG, "Removed neighbour: {}".format(node_id))

    def get(self, node_id):
        """Get a neighbour entry by ID."""
        return self._table.get(node_id)

    def get_all(self):
        """Return a copy of the full neighbour table."""
        return dict(self._table)

    def get_by_protocol(self, protocol):
        """Return all neighbours reachable via a specific protocol."""
        result = {}
        for nid, entry in self._table.items():
            if protocol in entry["protocols"]:
                result[nid] = entry
        return result

    def prune_dead(self):
        """Remove neighbours that haven't been seen within HELLO_TIMEOUT."""
        now = time.time()
        dead = []
        for nid, entry in self._table.items():
            if now - entry["last_seen"] > config.HELLO_TIMEOUT:
                dead.append(nid)
        for nid in dead:
            logger.warn(TAG, "Neighbour {} timed out, removing".format(nid))
            del self._table[nid]
        return dead

    def to_dict(self):
        """Serialize the table for MQTT topology broadcast."""
        return self._table

    def merge_remote(self, remote_node_id, remote_table):
        """Merge topology info received from another bridge.
        This extends our view to 2-hop neighbours."""
        for nid, entry in remote_table.items():
            if nid == config.NODE_ID:
                continue
            if nid not in self._table:
                # Mark as 2-hop neighbour (reachable via the remote bridge)
                self._table[nid] = {
                    "protocols": entry.get("protocols", []),
                    "rssi": entry.get("rssi", 0),
                    "last_seen": entry.get("last_seen", time.time()),
                    "capabilities": entry.get("capabilities", []),
                    "via": remote_node_id,  # indicates 2-hop
                }

    def __len__(self):
        return len(self._table)


def create_hello_payload():
    """Build a Hello message payload for broadcast."""
    return {
        "type": "hello",
        "node_id": config.NODE_ID,
        "role": config.NODE_ROLE,
        "capabilities": config.CAPABILITIES,
        "timestamp": time.time(),
    }


def parse_hello(data):
    """Parse a Hello message. Returns dict or None."""
    try:
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8")
        msg = json.loads(data)
        if msg.get("type") == "hello" and "node_id" in msg:
            return msg
    except (ValueError, KeyError):
        # Non-hello payloads are expected on this path.
        pass
    return None
