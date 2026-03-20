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


def main():
    logger.set_level(config.LOG_LEVEL)
    logger.info(TAG, "=== MPR Bridge starting: {} ===".format(config.NODE_ID))
    logger.info(TAG, "Role: {}, Capabilities: {}".format(config.NODE_ROLE, config.CAPABILITIES))

    # --- Shared state ---
    neighbour_table = NeighbourTable()
    routing_table = RoutingTable()
    ingress_queue = PriorityQueue("ingress")
    wifi_egress = PriorityQueue("wifi_egress")
    lora_egress = PriorityQueue("lora_egress")

    # --- Software watchdog ---
    def _on_watchdog():
        logger.error(TAG, "Watchdog triggered — resetting")
        try:
            import machine
            machine.reset()
        except Exception:
            pass

    watchdog = SoftwareWatchdog(timeout_ms=60000, callback=_on_watchdog)

    # --- Initialise interfaces ---
    lora_ok = False
    ble_ok = False
    wifi_ok = False

    if "LoRa" in config.CAPABILITIES:
        from interfaces import lora_interface
        lora_ok = lora_interface.init()

    if "WiFi" in config.CAPABILITIES or "MQTT" in config.CAPABILITIES:
        from interfaces import wifi_interface
        wifi_ok = wifi_interface.init()

    if "BLE" in config.CAPABILITIES:
        from interfaces import ble_interface
        ble_ok = ble_interface.init()

    logger.info(TAG, "Interfaces — LoRa:{} BLE:{} WiFi:{}".format(
        "OK" if lora_ok else "FAIL",
        "OK" if ble_ok else "FAIL",
        "OK" if wifi_ok else "FAIL",
    ))

    # --- Start async event loop ---
    import uasyncio as asyncio

    async def translator_task():
        """Core translator: pull from ingress, route, push to appropriate egress."""
        from core.translator import translate_to_mqtt
        logger.info(TAG, "Translator task started")

        while True:
            try:
                if not ingress_queue.is_empty():
                    pkt = ingress_queue.pop()
                    if pkt is None:
                        await asyncio.sleep_ms(50)
                        continue

                    dst = pkt.get("dst", "dashboard")
                    src = pkt.get("src", "unknown")

                    # If this packet is for us (the bridge), process/forward
                    if pkt.get("hop_dst") and pkt["hop_dst"] != config.NODE_ID:
                        # Not for us — drop or forward
                        logger.debug(TAG, "Packet not addressed to us, skipping")
                        await asyncio.sleep_ms(10)
                        continue

                    # Decrement TTL for relayed packets
                    if pkt.get("src") != config.NODE_ID:
                        pkt = packet.decrement_ttl(pkt)
                        if pkt is None:
                            continue

                    # Route lookup
                    route = routing_table.lookup(dst)
                    if route:
                        protocol = route["via_protocol"]
                        pkt["hop_src"] = config.NODE_ID
                        pkt["hop_dst"] = route["next_hop"]
                    else:
                        # Default: forward to WiFi/MQTT (dashboard)
                        protocol = "WiFi"

                    # Push to appropriate egress queue
                    priority = pkt.get("priority", packet.PRIORITY_NORMAL)
                    if protocol in ("WiFi", "MQTT"):
                        wifi_egress.push(priority, pkt)
                    elif protocol == "LoRa":
                        lora_egress.push(priority, pkt)
                    elif protocol == "BLE":
                        # BLE write-back is not yet supported; forward via WiFi
                        wifi_egress.push(priority, pkt)
                    else:
                        wifi_egress.push(priority, pkt)

                    logger.debug(TAG, "Routed pkt from {} to {} via {}".format(src, dst, protocol))

            except Exception as e:
                logger.error(TAG, "Translator error: {}".format(e))

            await asyncio.sleep_ms(50)

    async def route_maintenance_task():
        """Periodically prune dead neighbours and recompute routes."""
        logger.info(TAG, "Route maintenance task started")
        while True:
            try:
                # Prune dead neighbours
                dead = neighbour_table.prune_dead()
                if dead:
                    logger.warn(TAG, "Dead neighbours pruned: {}".format(dead))

                # Recompute routing table
                if len(neighbour_table) > 0:
                    routing_table.compute(neighbour_table)

                    # Check MPR status
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

        # Core tasks (always run)
        tasks.append(asyncio.create_task(translator_task()))
        tasks.append(asyncio.create_task(route_maintenance_task()))
        tasks.append(asyncio.create_task(watchdog_task()))

        # Interface-specific tasks
        if lora_ok:
            from interfaces import lora_interface
            tasks.append(asyncio.create_task(
                lora_interface.rx_task(ingress_queue, neighbour_table)))
            tasks.append(asyncio.create_task(
                lora_interface.tx_task(lora_egress)))
            tasks.append(asyncio.create_task(
                lora_interface.hello_task(neighbour_table)))

        if ble_ok:
            from interfaces import ble_interface
            tasks.append(asyncio.create_task(
                ble_interface.rx_task(ingress_queue, neighbour_table)))

        if wifi_ok:
            from interfaces import wifi_interface
            tasks.append(asyncio.create_task(
                wifi_interface.tx_task(wifi_egress)))
            tasks.append(asyncio.create_task(
                wifi_interface.rx_task(ingress_queue, neighbour_table)))
            tasks.append(asyncio.create_task(
                wifi_interface.hello_task(neighbour_table)))

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
        time.sleep(5)
        try:
            import machine
            machine.reset()
        except Exception:
            pass


# Auto-run on boot
main()
