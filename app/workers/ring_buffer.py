from __future__ import annotations

import threading
from typing import Any, List, Optional


class RingBuffer:
    """
    Thread-safe Circular Memory Buffer for zero-copy high-FPS frame grabbing.
    """

    def __init__(self, capacity: int = 30):
        self.capacity = capacity
        self.buffer: List[Optional[Any]] = [None] * capacity
        self.head = 0
        self.size = 0
        self.lock = threading.Lock()

    def push(self, item: Any) -> None:
        with self.lock:
            self.buffer[self.head] = item
            self.head = (self.head + 1) % self.capacity
            if self.size < self.capacity:
                self.size += 1

    def get_latest(self) -> Optional[Any]:
        with self.lock:
            if self.size == 0:
                return None
            idx = (self.head - 1 + self.capacity) % self.capacity
            return self.buffer[idx]

    def clear(self) -> None:
        with self.lock:
            self.buffer = [None] * self.capacity
            self.head = 0
            self.size = 0
