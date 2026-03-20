# core/mpr.py — MPR (Multi-Point Relay) selection and election logic
# Adapted from OLSR RFC 3626 with protocol-awareness extension

import config
from utils import logger

TAG = "MPR"


def select_mprs(self_id, neighbour_table):
    """Select the MPR set from 1-hop neighbours using the OLSR algorithm
    with a protocol-aware tie-breaking extension.

    Returns a set of node_ids that should act as MPRs.
    """
    all_neighbours = neighbour_table.get_all()

    # Separate 1-hop and 2-hop neighbours
    one_hop = {}
    two_hop = {}

    for nid, entry in all_neighbours.items():
        if nid == self_id:
            continue
        if entry.get("via"):
            # 2-hop neighbour (reachable only through another node)
            two_hop[nid] = entry
        else:
            # Direct 1-hop neighbour
            one_hop[nid] = entry

    if not two_hop:
        # No 2-hop neighbours — no MPRs needed
        logger.debug(TAG, "No 2-hop neighbours, MPR set is empty")
        return set()

    # Build a map: which 1-hop neighbours can reach which 2-hop neighbours
    # For simplicity, a 2-hop neighbour's "via" field tells us the relay
    reach_map = {}  # { 1-hop_id: set of 2-hop_ids }
    for nid_2hop, entry in two_hop.items():
        relay = entry.get("via")
        if relay and relay in one_hop:
            reach_map.setdefault(relay, set()).add(nid_2hop)

    uncovered_2hop = set(two_hop.keys())
    mpr_set = set()

    # Step 1: Cover isolated 2-hop nodes (reachable through only one 1-hop)
    reverse_map = {}  # { 2-hop_id: set of 1-hop relays }
    for relay, reachable in reach_map.items():
        for n2 in reachable:
            reverse_map.setdefault(n2, set()).add(relay)

    for n2, relays in reverse_map.items():
        if len(relays) == 1:
            sole_relay = list(relays)[0]
            mpr_set.add(sole_relay)
            uncovered_2hop -= reach_map.get(sole_relay, set())

    # Step 2: Greedy coverage — pick 1-hop that covers most remaining 2-hop
    while uncovered_2hop:
        best = None
        best_count = -1
        best_proto_score = -1

        for relay, reachable in reach_map.items():
            if relay in mpr_set:
                continue
            covered = len(reachable & uncovered_2hop)
            if covered == 0:
                continue

            # Protocol-aware tie-breaking: prefer more protocol capabilities
            proto_score = len(one_hop[relay].get("capabilities", []))

            if (covered > best_count) or \
               (covered == best_count and proto_score > best_proto_score):
                best = relay
                best_count = covered
                best_proto_score = proto_score

        if best is None:
            break  # Cannot cover remaining 2-hop nodes

        mpr_set.add(best)
        uncovered_2hop -= reach_map.get(best, set())

    logger.info(TAG, "MPR set: {}".format(mpr_set))
    return mpr_set


def is_mpr(self_id, neighbour_table):
    """Check if this node should act as an MPR.
    A node is an MPR if any other node has selected it as such,
    or if it's the best candidate in its local neighbourhood."""
    # In a simple deployment, the bridge is always the natural MPR
    if config.NODE_ROLE == "bridge":
        return True

    mprs = select_mprs(self_id, neighbour_table)
    return self_id in mprs
