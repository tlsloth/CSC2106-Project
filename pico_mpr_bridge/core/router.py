# core/router.py — Dijkstra routing with cross-protocol translation costs

import config
from utils import logger

TAG = "RTR"

# Cost lookup for protocol translation pairs
_COST_MAP = {
    ("LoRa", "LoRa"):  config.COST_NATIVE,
    ("WiFi-Direct", "WiFi-Direct"):  config.COST_NATIVE,
    ("BLE", "BLE"):    config.COST_NATIVE,
    ("MQTT", "MQTT"):  config.COST_NATIVE,
    ("WiFi-Direct", "MQTT"): 5,
    ("LoRa", "WiFi-Direct"):  config.COST_LORA_WIFI,
    ("WiFi-Direct", "LoRa"):  config.COST_LORA_WIFI,
    ("BLE", "WiFi-Direct"):   config.COST_BLE_WIFI,
    ("WiFi-Direct", "BLE"):   config.COST_BLE_WIFI,
    ("LoRa", "BLE"):   config.COST_LORA_BLE,
    ("BLE", "LoRa"):   config.COST_LORA_BLE,
}


def get_translation_cost(proto_a, proto_b):
    """Return the cost of translating between two protocols."""
    if proto_a == proto_b:
        return config.COST_NATIVE
    return _COST_MAP.get((proto_a, proto_b), 10)  # default high cost for unknown pairs

def _calculate_rssi_penalty(rssi):
    """Calculates penalty based on signal strength"""
    if rssi == 0:
        return 2
    if rssi >= -65:
        return 0
    elif rssi >= -85:
        return 1
    elif rssi >= -100:
        return 3
    else:
        return 6


def build_graph(neighbour_table, self_id=None):
    """Build a weighted graph from the neighbour table for Dijkstra.

    The graph is: { node_id: { neighbour_id: cost, ... }, ... }
    Cost is the minimum translation cost between the two nodes' shared protocols + rssi calculation.
    """
    if self_id is None:
        self_id = config.NODE_ID

    all_entries = neighbour_table.get_all()
    nodes = set([self_id]) | set(all_entries.keys())

    graph = {n: {} for n in nodes}

    # Edges from self to direct neighbours
    self_protos = config.CAPABILITIES
    for nid, entry in all_entries.items():
        if entry.get("via"):
            # 2-hop neighbour — don't add direct edge from self
            continue
        nbr_protos = entry.get("protocols", [])
        # Find minimum cost across protocol pairs
        rssi = entry.get("rssi",0)
        rssi_penalty = _calculate_rssi_penalty(rssi)
        
        min_cost = float("inf")
        for sp in self_protos:
            for np in nbr_protos:
                c = get_translation_cost(sp, np)
                if c < min_cost:
                    min_cost = c
        if min_cost < float("inf"):
            final_cost = min_cost + rssi_penalty
            graph[self_id][nid] = final_cost
            graph[nid][self_id] = final_cost

    # Edges between remote neighbours (from merged topology)
    for nid, entry in all_entries.items():
        via = entry.get("via")
        if via and via in graph:
            nbr_protos = entry.get("protocols", [])
            via_protos = all_entries.get(via, {}).get("protocols", [])
            rssi = entry.get("rssi", 0)
            rssi_penalty = _calculate_rssi_penalty(rssi)
            min_cost = float("inf")
            for vp in via_protos:
                for np in nbr_protos:
                    c = get_translation_cost(vp, np)
                    if c < min_cost:
                        min_cost = c
            if min_cost < float("inf"):
                final_cost = min_cost + rssi_penalty
                graph.setdefault(via, {})[nid] = final_cost
                graph.setdefault(nid, {})[via] = final_cost

    return graph


def dijkstra(graph, source):
    """Standard Dijkstra with translation-aware edge weights.

    Returns (dist, prev) dictionaries.
    """
    dist = {node: float("inf") for node in graph}
    prev = {node: None for node in graph}
    dist[source] = 0
    unvisited = set(graph.keys())

    while unvisited:
        # Pick unvisited node with smallest distance
        current = None
        best = float("inf")
        for n in unvisited:
            if dist[n] < best:
                best = dist[n]
                current = n
        if current is None or dist[current] == float("inf"):
            break
        unvisited.remove(current)

        for neighbour, edge_cost in graph[current].items():
            if neighbour not in unvisited:
                continue
            new_cost = dist[current] + edge_cost
            if new_cost < dist[neighbour]:
                dist[neighbour] = new_cost
                prev[neighbour] = current

    return dist, prev


def get_path(prev, target):
    """Reconstruct the shortest path from prev dict."""
    path = []
    node = target
    while node is not None:
        path.append(node)
        node = prev.get(node)
    return list(reversed(path))


class RoutingTable:
    """Computed routing table: for each destination, stores next_hop, protocol, and cost."""

    def __init__(self):
        # { dest_id: {"next_hop": str, "via_protocol": str, "cost": float} }
        self._table = {}

    def compute(self, neighbour_table):
        """Recompute the routing table from the current neighbour table."""
        graph = build_graph(neighbour_table)
        if not graph:
            logger.warn(TAG, "Empty graph, cannot compute routes")
            return

        dist, prev = dijkstra(graph, config.NODE_ID)

        self._table.clear()
        all_entries = neighbour_table.get_all()

        for dest, cost in dist.items():
            if dest == config.NODE_ID or cost == float("inf"):
                continue

            path = get_path(prev, dest)
            if len(path) < 2:
                continue

            next_hop = path[1]

            # Determine the protocol to use for the next hop
            via_protocol = _determine_protocol(next_hop, all_entries)

            self._table[dest] = {
                "next_hop": next_hop,
                "via_protocol": via_protocol,
                "cost": cost,
            }

        logger.info(TAG, "Routing table updated: {} destinations".format(len(self._table)))
        for dest, entry in self._table.items():
            logger.debug(TAG, "  {} -> next={} via={} cost={}".format(
                dest, entry["next_hop"], entry["via_protocol"], entry["cost"]))

    def lookup(self, destination):
        """Look up routing info for a destination. Returns dict or None."""
        return self._table.get(destination)

    def get_all(self):
        return dict(self._table)

    def __len__(self):
        return len(self._table)


def _determine_protocol(next_hop, all_entries):
    """Determine the best protocol to reach the next hop."""
    entry = all_entries.get(next_hop)
    if not entry:
        return "WiFi-Direct"  # fallback

    protocols = entry.get("protocols", [])
    my_protos = config.CAPABILITIES

    # Prefer native protocols in order: LoRa > BLE > WiFi
    for preferred in ["LoRa", "BLE", "WiFi", "MQTT"]:
        if preferred in protocols and preferred in my_protos:
            return preferred

    return protocols[0] if protocols else "WiFi"
