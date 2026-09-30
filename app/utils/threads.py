"""Keeps background worker threads alive.

Camera readers, inference workers, stream publishers, QR readers and the
vision runner each run a loop on their own thread. If a loop raises, the
thread would end quietly while the API keeps answering, so counting or a
camera feed stops with nothing in the log. run_supervised logs the error with
its traceback and starts the loop again after a short, growing pause.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_restarts: Dict[str, int] = {}


def restart_counts() -> Dict[str, int]:
    """Restarts per kind of worker since the server started."""
    with _lock:
        return dict(_restarts)


def run_supervised(
    name: str,
    loop: Callable[[], None],
    stop: threading.Event,
    *,
    kind: str = "worker",
    first_delay: float = 1.0,
    max_delay: float = 30.0,
) -> None:
    """Run loop() until it returns or stop is set, restarting it after an unexpected exception."""
    delay = first_delay
    while not stop.is_set():
        started = time.monotonic()
        try:
            loop()
            return
        except Exception:
            if stop.is_set():
                return
            # A loop that ran for a while before failing is not crash-looping.
            if time.monotonic() - started > 60.0:
                delay = first_delay
            with _lock:
                _restarts[kind] = _restarts.get(kind, 0) + 1
            logger.exception("Worker %s crashed; restarting it in %.0f s", name, delay)
        if stop.wait(delay):
            return
        delay = min(delay * 2, max_delay)
