# utils/watchdog.py — Software watchdog / heartbeat monitor

import time
from utils import logger

TAG = "WDT"


class SoftwareWatchdog:
    """Simple software watchdog that tracks last-fed time and triggers
    a callback (e.g. machine.reset) if not fed within the timeout."""

    def __init__(self, timeout_ms=30000, callback=None):
        self.timeout_ms = timeout_ms
        self.callback = callback
        self._last_feed = time.ticks_ms()

    def feed(self):
        self._last_feed = time.ticks_ms()

    def check(self):
        elapsed = time.ticks_diff(time.ticks_ms(), self._last_feed)
        if elapsed > self.timeout_ms:
            logger.error(TAG, "Watchdog timeout! Elapsed: {} ms".format(elapsed))
            if self.callback:
                self.callback()
            return True
        return False
