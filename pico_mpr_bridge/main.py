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
        """Core translator: pull from ingress, route, push to appropriate egress."""
        from core.translator import translate_to_mqtt
        logger.info(TAG, "Translator task started")

        pending_routes = {}
        last_query_time = {}

        while True:
            try:
                if not ingress_queue.is_empty():
                    pkt = ingress_queue.pop()

                    if pkt is None:
                        await asyncio.sleep_ms(50)
                        continue

                    if pkt.get("type") == "route_resp":
                        target_dst = pkt.get("dst") # The destination we originally asked about
                        status = pkt.get("status")
                        
                        # Only process it if we were the bridge that asked, and a route was found!
                        if pkt.get("req_src") == config.NODE_ID and status == "ok":
                            next_hop = pkt.get("next_hop")
                            via_proto = pkt.get("via_protocol")
                            cost = pkt.get("cost", 10)
                            
                            if target_dst and next_hop:
                                # 1. Memorize the route in our new cache!
                                routing_table.cache_route(target_dst, next_hop, via_proto, cost)
                                
                                # 2. Flush the Holding Pen!
                                if target_dst in pending_routes:
                                    logger.info(TAG, f"Flushing {len(pending_routes[target_dst])} held packets for {target_dst}!")
                                    for held_pkt in pending_routes[target_dst]:
                                        # Push back into ingress with high priority so it routes instantly
                                        ingress_queue.push(10, held_pkt) 
                                    del pending_routes[target_dst]
                        
                        # Consume the response packet so it doesn't get routed further
                        continue
                    dst = pkt.get("dst")
                    src = pkt.get("src") or pkt.get("node_id") or "unknown"
                    hop_dst = pkt.get("hop_dst")

                    if not dst:
                        logger.warn(f"Packet from {src} has no destination, dropping packet.")
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
                        # query route if we dont have it, this should only be done if we dont have wifi direct and lora / ble available
                        logger.info(TAG, f"No route to {dst}, Holding packets for query.")
                        if dst not in pending_routes:
                            pending_routes[dst] = []
                            pending_routes[dst].append(pkt)

                        now = time.time()
                        if dst not in last_query_time or (now - last_query_time[dst] > 5):
                            # create query pkt
                            query_pkt = {
                                "type": "route_query",
                                "kind": "control",
                                "src": config.NODE_ID,
                                "dst": dst
                            }

                            # check if we support lora or ble
                            if "LoRa" in config.CAPABILITIES:
                                # push with priority
                                lora_egress.push(1, query_pkt)
                                logger.debug(TAG, f"Broadcasted route_query for {dst} over LoRa")

                            #ble check

                            last_query_time[dst] = now
                        continue

                    # INJECTION 2: Route packets to the new UDP queue
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
            tasks.append(asyncio.create_task(
                ble_interface.rx_task(ingress_queue, ble_egress, neighbour_table)))  # ← must be here
            tasks.append(asyncio.create_task(
                ble_interface.tx_task(ble_egress, neighbour_table)))
            tasks.append(asyncio.create_task(
                ble_interface.hello_task()))

               
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