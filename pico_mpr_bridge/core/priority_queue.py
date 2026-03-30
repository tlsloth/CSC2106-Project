# core/priority_queue.py — Heapq-based priority queue for packet scheduling

import heapq
import time
from utils import logger

TAG = "PQ"


class PriorityQueue:
    """A min-heap priority queue with a maximum size to prevent OOM.
    Lower priority value = higher urgency.
    Items are tuples of (priority, timestamp, item) to ensure stable ordering."""

    def __init__(self, name="default", max_size=100): 
        self._heap = []
        self._name = name
        self._max_size = max_size 

    def push(self, priority, item):
        """Push an item with the given priority (0 = most urgent).
        If the queue is full, drops the lowest-priority item."""
        entry = (priority, time.ticks_ms(), item)

        if len(self._heap) < self._max_size:
            heapq.heappush(self._heap, entry)
            logger.debug(TAG, "{}: pushed prio={} (size={})".format(
                self._name, priority, len(self._heap)))
        else:
            # The queue is full. Find the LEAST urgent item.
            # max() finds the highest priority number (lowest urgency).
            least_urgent = max(self._heap)

            # Check if the incoming packet is more urgent than the worst packet in the queue
            if entry < least_urgent:
                # Remove the worst packet and re-balance the heap
                self._heap.remove(least_urgent)
                heapq.heapify(self._heap)
                
                # Push the new, more important packet
                heapq.heappush(self._heap, entry)
                logger.warn(TAG, "{}: Queue full! Dropped prio={} to make room for prio={}".format(
                    self._name, least_urgent[0], priority))
            else:
                # The incoming packet is too low priority to care about right now
                logger.warn(TAG, "{}: Queue full! Dropped incoming prio={} (too low)".format(
                    self._name, priority))

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