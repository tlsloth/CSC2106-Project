# core/priority_queue.py — Heapq-based priority queue for packet scheduling

import heapq
import time
from utils import logger

TAG = "PQ"


class PriorityQueue:
    """A min-heap priority queue. Lower priority value = higher urgency.
    Items are tuples of (priority, timestamp, item) to ensure stable ordering."""

    def __init__(self, name="default"):
        self._heap = []
        self._name = name

    def push(self, priority, item):
        """Push an item with the given priority (0 = most urgent)."""
        entry = (priority, time.ticks_ms(), item)
        heapq.heappush(self._heap, entry)
        logger.debug(TAG, "{}: pushed prio={} (size={})".format(
            self._name, priority, len(self._heap)))

    def pop(self):
        """Pop and return the highest-priority (lowest value) item.
        Returns None if the queue is empty."""
        if self._heap:
            priority, ts, item = heapq.heappop(self._heap)
            return item
        return None

    def peek(self):
        """Peek at the highest-priority item without removing it."""
        if self._heap:
            return self._heap[0][2]
        return None

    def __len__(self):
        return len(self._heap)

    def is_empty(self):
        return len(self._heap) == 0

    def clear(self):
        self._heap.clear()
