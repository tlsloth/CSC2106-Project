# main.py — Entry point: init hardware, start asyncio loop
# Protocol-Aware MPR Bridge for Raspberry Pi Pico W + LoRa Shield

import time
import config
from utils import logger
from utils.watchdog import SoftwareWatchdog
from core.neighbour import NeighbourTable
from core.router import RoutingTable
from core.priority_queue import PriorityQueue
from core import packet, mpr

TAG = "MAIN"


def _load_lora_interface():
    transport = getattr(config, "LORA_TRANSPORT", "SPI").upper()
    if transport == "UART":
        from interfaces import uart_lora_interface
        return transport, uart_lora_interface

    from interfaces import lora_interface
    return transport, lora_interface


def main():
    logger.set_level(config.LOG_LEVEL)
    logger.info(TAG, "=== MPR Bridge starting: {} ===".format(config.NODE_ID))
    logger.info(TAG, "Role: {}, Capabilities: {}".format(config.NODE_ROLE, config.CAPABILITIES))

    # --- Shared state ---
    neighbour_table = NeighbourTable()
    routing_table = RoutingTable()
    ingress_queue = PriorityQueue("ingress")
    
    # Egress Queues for each protocol
    wifi_egress = PriorityQueue("wifi_egress")           # For MQTT to Dashboard
    lora_egress = PriorityQueue("lora_egress")           # For LoRa Mesh
    wifi_direct_egress = PriorityQueue("wifi_direct")    # For UDP Peer-to-Peer
    ble_egress = PriorityQueue("ble_egress")             # For BLE Mesh (future)

    # --- Software watchdog ---
    def _on_watchdog():
        logger.error(TAG, "Watchdog triggered — resetting")
        if getattr(config, "WATCHDOG_RESET_ON_TIMEOUT", False):
            try:
                import machine
                machine.reset()
            except Exception:
                pass
        else:
            logger.warn(TAG, "Watchdog timeout occurred; reset suppressed by config")

    watchdog = SoftwareWatchdog(timeout_ms=60000, callback=_on_watchdog)

    # --- Initialise interfaces ---
    lora_ok = False
    ble_ok = False
    wifi_ok = False
    wifi_direct_ok = False
    
    lora_transport = getattr(config, "LORA_TRANSPORT", "SPI").upper()
    lora_module = None

    if "LoRa" in config.CAPABILITIES:
        lora_transport, lora_module = _load_lora_interface()
        lora_ok = lora_module.init()

    if "WiFi" in config.CAPABILITIES or "MQTT" in config.CAPABILITIES:
        from interfaces import wifi_interface
        wifi_ok = wifi_interface.init()

    # INJECTION 1: Initialize WiFi-Direct
    if "WiFi-Direct" in config.CAPABILITIES:
        from interfaces import wifi_direct_interface
        wifi_direct_ok = wifi_direct_interface.init()

    if "BLE" in config.CAPABILITIES:
        from interfaces import ble_interface
        ble_ok = ble_interface.init()

    logger.info(TAG, "Interfaces — LoRa({}):{} BLE:{} WiFi:{} WiFi-Direct:{}".format(
        lora_transport,
        "OK" if lora_ok else "FAIL",
        "OK" if ble_ok else "FAIL",
        "OK" if wifi_ok else "FAIL",
        "OK" if wifi_direct_ok else "FAIL"
    ))

    # --- Start async event loop ---
    import uasyncio as asyncio

    async def translator_task():
        """Core translator: pull from ingress, route, push to appropriate egress.
        Implements AODV-style reactive route discovery with query forwarding."""
        from core.translator import translate_to_mqtt
        logger.info(TAG, "Translator task started")

        pending_routes = {}
        last_query_time = {}

        # AODV state for multi-hop route discovery
        _seen_queries = {}      # (src, dst, seq) -> timestamp — dedup queries
        _reverse_paths = {}     # (req_src, dst) -> (protocol, timestamp) — reverse path for resp relay
        _seen_responses = {}    # (req_src, dst, seq) -> timestamp — dedup responses
        _query_seq = [0]        # mutable counter for outgoing query sequence numbers
        _prune_counter = [0]

        def _next_query_seq():
            _query_seq[0] = (_query_seq[0] + 1) % 65536
            return _query_seq[0]

        def _prune_seen_caches():
            """Remove stale AODV cache entries every ~100 loop iterations."""
            _prune_counter[0] += 1
            if _prune_counter[0] < 100:
                return
            _prune_counter[0] = 0
            now = time.time()
            for cache in (_seen_queries, _seen_responses):
                expired = [k for k, ts in cache.items() if now - ts > 120]
                for k in expired:
                    del cache[k]
            expired_rp = [k for k, v in _reverse_paths.items() if now - v[1] > 120]
            for k in expired_rp:
                del _reverse_paths[k]

        def _push_to_protocol(protocol, pkt):
            """Push a packet to the egress queue for the given protocol."""
            if protocol == "LoRa" and "LoRa" in config.CAPABILITIES:
                lora_egress.push(1, pkt)
            elif protocol == "WiFi-Direct" and "WiFi-Direct" in config.CAPABILITIES:
                wifi_direct_egress.push(1, pkt)
            else:
                # Fallback: flood on all available interfaces
                if "LoRa" in config.CAPABILITIES:
                    lora_egress.push(1, pkt)
                if "WiFi-Direct" in config.CAPABILITIES:
                    wifi_direct_egress.push(1, dict(pkt))

        def _flood_all_interfaces(pkt):
            """Broadcast a packet on every available interface."""
            if "LoRa" in config.CAPABILITIES:
                lora_egress.push(1, pkt)
            if "WiFi-Direct" in config.CAPABILITIES:
                wifi_direct_egress.push(1, dict(pkt))

        while True:
            try:
                if not ingress_queue.is_empty():
                    pkt = ingress_queue.pop()

                    if pkt is None:
                        await asyncio.sleep_ms(50)
                        continue

                    _prune_seen_caches()
                    pkt_type = pkt.get("type")

                    # ==============================================
                    # AODV: Handle incoming route_query (forwarding)
                    # ==============================================
                    if pkt_type == "route_query":
                        query_src = pkt.get("src", "")
                        query_dst = pkt.get("dst", "")
                        query_seq = pkt.get("seq", 0)
                        query_ttl = pkt.get("ttl", config.PACKET_TTL)
                        rx_proto = pkt.get("_rx_protocol", "")

                        # Ignore our own queries bouncing back
                        if query_src == config.NODE_ID:
                            continue

                        # Dedup: drop if we've seen this exact query before
                        query_key = (query_src, query_dst, query_seq)
                        if query_key in _seen_queries:
                            continue
                        _seen_queries[query_key] = time.time()

                        # Record reverse path so we can relay the response back
                        _reverse_paths[(query_src, query_dst)] = (rx_proto, time.time())

                        # Can we answer this query?
                        route = None
                        if query_dst == config.NODE_ID:
                            # We ARE the destination
                            route = {"next_hop": config.NODE_ID, "via_protocol": "LOCAL", "cost": 0}
                        else:
                            route = routing_table.lookup(query_dst)

                        if route:
                            # We can answer — send route_resp back toward the querier
                            resp = {
                                "type": "route_resp",
                                "kind": "control",
                                "req_src": query_src,
                                "dst": query_dst,
                                "next_hop": config.NODE_ID,  # "to reach dst, come through me"
                                "via_protocol": rx_proto or "LoRa",
                                "cost": route["cost"],
                                "status": "ok",
                                "seq": query_seq,
                            }
                            _push_to_protocol(rx_proto, resp)
                            logger.info(TAG, "Answered route_query: {} from {}".format(query_dst, query_src))
                        else:
                            # Can't answer — forward the query if TTL allows
                            if query_ttl <= 1:
                                logger.debug(TAG, "route_query for {} TTL expired, dropping".format(query_dst))
                                continue

                            fwd = {
                                "type": "route_query",
                                "kind": "control",
                                "src": query_src,   # preserve original querier
                                "dst": query_dst,
                                "ttl": query_ttl - 1,
                                "seq": query_seq,
                            }
                            _flood_all_interfaces(fwd)
                            logger.info(TAG, "Forwarded route_query: {} from {} TTL={}".format(
                                query_dst, query_src, query_ttl - 1))

                        continue

                    # =====================================================
                    # AODV: Handle incoming route_resp (cache + relay)
                    # =====================================================
                    if pkt_type == "route_resp":
                        req_src = pkt.get("req_src", "")
                        target_dst = pkt.get("dst", "")
                        status = pkt.get("status", "")
                        relay_node = pkt.get("next_hop", "")
                        cost = pkt.get("cost", 999)
                        query_seq = pkt.get("seq", 0)
                        rx_proto = pkt.get("_rx_protocol", "LoRa")

                        # Dedup: avoid processing the same response twice
                        resp_key = (req_src, target_dst, query_seq)
                        if resp_key in _seen_responses:
                            continue
                        _seen_responses[resp_key] = time.time()

                        # Cache the discovered route for ourselves
                        # "To reach target_dst, go through relay_node via rx_proto"
                        if status == "ok" and relay_node:
                            routing_table.cache_route(target_dst, relay_node, rx_proto, cost)
                            logger.info(TAG, "Cached route: {} -> next_hop={} via {} cost={}".format(
                                target_dst, relay_node, rx_proto, cost))

                        if req_src == config.NODE_ID:
                            # This response is for US — flush pending packets
                            if status == "ok" and target_dst in pending_routes:
                                logger.info(TAG, "Flushing {} held packets for {}".format(
                                    len(pending_routes[target_dst]), target_dst))
                                for held_pkt in pending_routes[target_dst]:
                                    ingress_queue.push(10, held_pkt)
                                del pending_routes[target_dst]
                        elif status == "ok":
                            # Not for us — relay the response toward the original querier
                            fwd_resp = dict(pkt)
                            fwd_resp["next_hop"] = config.NODE_ID  # update relay identity
                            fwd_resp["cost"] = cost + 200          # accumulate hop cost
                            fwd_resp.pop("_rx_protocol", None)

                            # Use the reverse path recorded during query flooding
                            rp = _reverse_paths.get((req_src, target_dst))
                            reverse_proto = rp[0] if rp else ""
                            _push_to_protocol(reverse_proto, fwd_resp)
                            logger.info(TAG, "Relayed route_resp: {} toward {}".format(target_dst, req_src))

                        continue

                    # ==============================================
                    # Regular packet processing
                    # ==============================================
                    dst = pkt.get("dst")
                    src = pkt.get("src") or pkt.get("node_id") or "unknown"
                    hop_dst = pkt.get("hop_dst")

                    if not dst:
                        logger.warn(TAG, "Packet from {} has no destination, dropping.".format(src))
                        continue
                    if (
                        src != config.NODE_ID
                        and hop_dst
                        and hop_dst != config.NODE_ID
                        ):
                        logger.debug(TAG, "Packet not addressed to us, dropping.")
                        continue

                    if pkt.get("src") != config.NODE_ID:
                        pkt = packet.decrement_ttl(pkt)
                        if pkt is None:
                            continue

                    route = routing_table.lookup(dst)
                    if route:
                        protocol = route["via_protocol"]
                        pkt["hop_src"] = config.NODE_ID
                        pkt["hop_dst"] = route["next_hop"]
                    else:
                        # No route — hold packet and initiate AODV route query
                        logger.info(TAG, "No route to {}, holding packet for query.".format(dst))
                        if dst not in pending_routes:
                            pending_routes[dst] = []
                        pending_routes[dst].append(pkt)

                        now = time.time()
                        if dst not in last_query_time or (now - last_query_time[dst] > 15):
                            query_pkt = {
                                "type": "route_query",
                                "kind": "control",
                                "src": config.NODE_ID,
                                "dst": dst,
                                "ttl": config.PACKET_TTL,
                                "seq": _next_query_seq(),
                            }
                            _flood_all_interfaces(query_pkt)
                            logger.info(TAG, "Broadcasted route_query for {}".format(dst))
                            last_query_time[dst] = now
                        continue

                    # Route packets to the appropriate egress queue
                    priority = pkt.get("priority", packet.PRIORITY_NORMAL)
                    if protocol in ("WiFi", "MQTT"):
                        wifi_egress.push(priority, pkt)
                    elif protocol == "WiFi-Direct":
                        wifi_direct_egress.push(priority, pkt)
                    elif protocol == "LoRa":
                        lora_egress.push(priority, pkt)
                    elif protocol == "BLE":
                        ble_egress.push(priority, pkt)
                    else:
                        wifi_direct_egress.push(priority, pkt)

                    logger.debug(TAG, "Routed pkt from {} to {} via {}".format(src, dst, protocol))

            except Exception as e:
                logger.error(TAG, "Translator error: {}".format(e))

            # ===================================================
            # Periodic retry: re-query for unresolved pending routes
            # Runs every loop iteration but throttled by last_query_time
            # ===================================================
            if pending_routes:
                now = time.time()
                for pending_dst in list(pending_routes.keys()):
                    # Check if a route was discovered since we last checked
                    route = routing_table.lookup(pending_dst)
                    if route:
                        logger.info(TAG, "Route now available for {}, flushing {} held packets".format(
                            pending_dst, len(pending_routes[pending_dst])))
                        for held_pkt in pending_routes[pending_dst]:
                            ingress_queue.push(10, held_pkt)
                        del pending_routes[pending_dst]
                        last_query_time.pop(pending_dst, None)
                        continue

                    # Re-query if cooldown has elapsed
                    if pending_dst not in last_query_time or (now - last_query_time[pending_dst] > 15):
                        query_pkt = {
                            "type": "route_query",
                            "kind": "control",
                            "src": config.NODE_ID,
                            "dst": pending_dst,
                            "ttl": config.PACKET_TTL,
                            "seq": _next_query_seq(),
                        }
                        _flood_all_interfaces(query_pkt)
                        logger.info(TAG, "Retrying route_query for {} ({} packets held)".format(
                            pending_dst, len(pending_routes[pending_dst])))
                        last_query_time[pending_dst] = now

                    # Cap held packets to prevent memory growth
                    if len(pending_routes.get(pending_dst, [])) > 10:
                        dropped = len(pending_routes[pending_dst]) - 10
                        pending_routes[pending_dst] = pending_routes[pending_dst][-10:]
                        logger.warn(TAG, "Dropped {} oldest held packets for {}".format(dropped, pending_dst))

            await asyncio.sleep_ms(50)

    async def route_maintenance_task():
        """Periodically prune dead neighbours and recompute routes."""
        logger.info(TAG, "Route maintenance task started")
        while True:
            try:
                dead = neighbour_table.prune_dead()
                if dead:
                    logger.warn(TAG, "Dead neighbours pruned: {}".format(dead))

                if len(neighbour_table) > 0:
                    routing_table.compute(neighbour_table)

                    if mpr.is_mpr(config.NODE_ID, neighbour_table):
                        logger.debug(TAG, "This node is an active MPR")

                watchdog.feed()
            except Exception as e:
                logger.error(TAG, "Route maintenance error: {}".format(e))

            await asyncio.sleep(config.HELLO_INTERVAL)

    async def watchdog_task():
        """Periodically check the software watchdog."""
        while True:
            watchdog.check()
            await asyncio.sleep(10)

    async def run():
        """Launch all concurrent tasks."""
        tasks = []

        # Core tasks
        tasks.append(asyncio.create_task(translator_task()))
        tasks.append(asyncio.create_task(route_maintenance_task()))
        tasks.append(asyncio.create_task(watchdog_task()))

        # LoRa Tasks
        should_run_lora_tasks = bool(lora_ok)
        if (not should_run_lora_tasks) and lora_module is not None and lora_transport in ("UART", "I2C"):
            should_run_lora_tasks = True

        if should_run_lora_tasks:
            if lora_transport == "UART":
                tasks.append(asyncio.create_task(
                    lora_module.rx_task(ingress_queue, lora_egress, neighbour_table, routing_table)))
            else:
                tasks.append(asyncio.create_task(
                    lora_module.rx_task(ingress_queue, neighbour_table)))
                
            tasks.append(asyncio.create_task(
                lora_module.tx_task(lora_egress)))
            if getattr(config, "ENABLE_LORA_HELLO", False):
                tasks.append(asyncio.create_task(
                    lora_module.hello_task(neighbour_table,lora_egress)))

        # BLE Tasks
        if ble_ok:
            from interfaces import ble_interface
            tasks.append(asyncio.create_task(
                ble_interface.rx_task(ingress_queue, neighbour_table)))
            tasks.append(asyncio.create_task(
                ble_interface.tx_task(ble_egress)))
            tasks.append(asyncio.create_task(
                ble_interface.hello_task(neighbour_table)))
            tasks.append(asyncio.create_task(
                ble_interface.rx_server_task(ingress_queue, neighbour_table)))
               

        # MQTT / WiFi Tasks
        if wifi_ok:
            from interfaces import wifi_interface
            tasks.append(asyncio.create_task(
                wifi_interface.tx_task(wifi_egress)))
            tasks.append(asyncio.create_task(
                wifi_interface.rx_task(ingress_queue, neighbour_table)))
            if getattr(config, "ENABLE_WIFI_HELLO", True):
                tasks.append(asyncio.create_task(
                    wifi_interface.hello_task(neighbour_table)))

        # INJECTION 3: Launch WiFi-Direct Tasks
        if wifi_direct_ok:
            from interfaces import wifi_direct_interface
            tasks.append(asyncio.create_task(
                wifi_direct_interface.tx_task(wifi_direct_egress)))
            tasks.append(asyncio.create_task(
                wifi_direct_interface.rx_task(ingress_queue, neighbour_table)))
            tasks.append(asyncio.create_task(
                wifi_direct_interface.hello_task(neighbour_table)))

        logger.info(TAG, "All tasks launched ({} total)".format(len(tasks)))

        # Run forever
        await asyncio.gather(*tasks)

    # Start the event loop
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info(TAG, "Shutdown requested")
    except Exception as e:
        logger.error(TAG, "Fatal error: {}".format(e))
        if getattr(config, "AUTO_RESET_ON_FATAL", False):
            time.sleep(2)
            try:
                import machine
                machine.reset()
            except Exception:
                pass
        else:
            logger.warn(TAG, "Auto-reset on fatal is disabled; staying at REPL")

# Auto-run on boot
main()