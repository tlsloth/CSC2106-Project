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


def _safe_mode_requested():
    """Allow skipping app startup by holding BOOTSEL during a short grace window."""
    if not getattr(config, "ALLOW_BOOTSEL_SAFE_MODE", True):
        return False

    grace_ms = int(getattr(config, "STARTUP_GRACE_MS", 0) or 0)
    if grace_ms <= 0:
        return False

    try:
        import rp2
    except Exception:
        return False

    logger.warn(TAG, "Hold BOOTSEL to enter safe mode ({} ms window)".format(grace_ms))
    start = time.ticks_ms()

    while time.ticks_diff(time.ticks_ms(), start) < grace_ms:
        try:
            if rp2.bootsel_button():
                logger.warn(TAG, "Safe mode requested via BOOTSEL; app startup skipped")
                return True
        except Exception:
            return False
        time.sleep_ms(100)

    return False


def _load_lora_interface():
    transport = getattr(config, "LORA_TRANSPORT", "SPI").upper()
    if transport == "UART":
        from interfaces import uart_lora_interface
        return transport, uart_lora_interface

    if transport == "I2C":
        from interfaces import i2c_lora_interface
        return transport, i2c_lora_interface

    from interfaces import lora_interface
    return transport, lora_interface


def main():
    logger.set_level(config.LOG_LEVEL)

    if _safe_mode_requested():
        logger.warn(TAG, "Safe mode active. Staying at REPL for recovery.")
        return

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
    lora_transport = getattr(config, "LORA_TRANSPORT", "SPI").upper()
    lora_module = None

    if "LoRa" in config.CAPABILITIES:
        lora_transport, lora_module = _load_lora_interface()
        lora_ok = lora_module.init()

    if "WiFi" in config.CAPABILITIES or "MQTT" in config.CAPABILITIES:
        from interfaces import wifi_interface
        wifi_ok = wifi_interface.init()

    if "BLE" in config.CAPABILITIES:
        from interfaces import ble_interface
        ble_ok = ble_interface.init()

    logger.info(TAG, "Interfaces — LoRa({}):{} BLE:{} WiFi:{}".format(
        lora_transport,
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

                    # Only drop transit packets that are explicitly for a different hop.
                    # Dashboard-bound telemetry is always consumed by this bridge for MQTT publish,
                    # even if hop_dst is set by a simple endpoint sender.
                    is_dashboard_bound = (dst == "dashboard")
                    if (
                        pkt.get("src") != config.NODE_ID
                        and pkt.get("hop_dst")
                        and pkt["hop_dst"] != config.NODE_ID
                        and not is_dashboard_bound
                    ):
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
        should_run_lora_tasks = bool(lora_ok)
        if (not should_run_lora_tasks) and lora_module is not None and lora_transport in ("UART", "I2C"):
            # UART/I2C bridge interfaces can recover from transient init failures at runtime.
            should_run_lora_tasks = True

        if should_run_lora_tasks:
            tasks.append(asyncio.create_task(
                lora_module.rx_task(ingress_queue, neighbour_table)))
            tasks.append(asyncio.create_task(
                lora_module.tx_task(lora_egress)))
            if getattr(config, "ENABLE_LORA_HELLO", False):
                tasks.append(asyncio.create_task(
                    lora_module.hello_task(neighbour_table)))

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
            if getattr(config, "ENABLE_WIFI_HELLO", True):
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
