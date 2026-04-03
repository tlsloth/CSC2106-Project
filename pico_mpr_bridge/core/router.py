# core/router.py — Dijkstra routing with cross-protocol translation costs

import config
from utils import logger
import time

TAG = "RTR"

# Protocol Base Costs (Lower = Higher Bandwidth / Preference)
_PROTOCOL_BASE_COST = {
    "MQTT": 10,
    "LOCAL": 10,
    "WiFi-Direct": 15,
    "WiFi": 15,
    "BLE": 50,
    "LoRa": 200
}

def _calculate_rssi_multiplier(rssi):
    """Calculates penalty multiplier based on signal strength."""
    if rssi == 0:  # Usually means local, MQTT, or unknown (assume perfect)
        return 1.0
    if rssi >= -65:
        return 1.0
    elif rssi >= -85:
        return 1.2
    elif rssi >= -100:
        return 1.5
    else:
        return 2.0

def get_edge_cost(protos_a, protos_b, rssi):
    """Finds the best shared protocol and calculates the physical link cost."""
    # To communicate over the air, nodes MUST share at least one protocol
    shared_protocols = set(protos_a).intersection(set(protos_b))
    
    if not shared_protocols:
        return float("inf") # No shared language over the air!
        
    # Find the absolute fastest (cheapest) shared protocol
    min_base_cost = float("inf")
    for p in shared_protocols:
        cost = _PROTOCOL_BASE_COST.get(p, 500) # Default huge cost for unknown
        if cost < min_base_cost:
            min_base_cost = cost
            
    multiplier = _calculate_rssi_multiplier(rssi)
    return min_base_cost * multiplier

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
# 1. Edges from self to direct neighbours
    self_protos = config.CAPABILITIES
    for nid, entry in all_entries.items():
        if entry.get("via"):
            continue # 2-hop neighbour
            
        nbr_protos = entry.get("protocols", [])
        rssi = entry.get("rssi", 0)
        
        # NEW MATH: Calculate link cost based on shared protocols and RSSI
        final_cost = get_edge_cost(self_protos, nbr_protos, rssi)
        
        if final_cost < float("inf"):
            graph[self_id][nid] = final_cost
            graph[nid][self_id] = final_cost

    # 2. Edges between remote neighbours (from merged topology)

    for nid, entry in all_entries.items():
        via = entry.get("via")
        if via and via in graph:
            nbr_protos = entry.get("capabilities", []) 
            via_protos = all_entries.get(via, {}).get("capabilities", []) 
            
            rssi = entry.get("rssi", 0)
            
            # NEW MATH: Calculate link cost
            final_cost = get_edge_cost(via_protos, nbr_protos, rssi)
            
            if final_cost < float("inf"):
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
        self._cache = {}

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

    def cache_route(self, destination, next_hop, via_protocol, cost, ttl_seconds=300):
        """Temporarily cache a route discovered via a route_query/route_resp."""
        self._cache[destination] = {
            "next_hop": next_hop,
            "via_protocol": via_protocol,
            "cost": cost,
            "expires_at": time.time() + ttl_seconds
        }
        logger.info(TAG, f"Cached reactive route: {destination} via {next_hop} ({via_protocol}) for {ttl_seconds}s")

    def lookup(self, destination):
        """Look up routing info. Checks proactive table first, then reactive cache."""
        # 1. Check the Proactive Table (Dijkstra)
        if destination in self._table:
            return self._table[destination]
            
        # 2. Check the Reactive Cache (AODV)
        if destination in self._cache:
            entry = self._cache[destination]
            if time.time() < entry["expires_at"]:
                return entry
            else:
                # Route has expired, prune it
                logger.debug(TAG, f"Cached route to {destination} expired.")
                del self._cache[destination]
                
        return None

    def get_all(self):
        return dict(self._table)

    def __len__(self):
        return len(self._table)


def _determine_protocol(next_hop, all_entries):
    """Determine the fastest protocol to reach the next hop."""
    entry = all_entries.get(next_hop)
    if not entry:
        return config.CAPABILITIES[0] if config.CAPABILITIES else "LoRa"  # fallback to own capability

    protocols = entry.get("protocols", [])
    my_protos = config.CAPABILITIES

    # Prefer high-bandwidth protocols first
    for preferred in ["MQTT", "WiFi-Direct", "WiFi", "BLE", "LoRa"]:
        if preferred in protocols and preferred in my_protos:
            return preferred

    return protocols[0] if protocols else (config.CAPABILITIES[0] if config.CAPABILITIES else "LoRa")
