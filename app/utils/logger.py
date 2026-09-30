from __future__ import annotations

import logging
import re
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Hashable, Tuple

from app.config import settings

# Credentials that end up inside logged text: rtsp://user:pass@camera,
# ?token=..., "password": "...". The value is replaced, the key is kept.
_URL_PASSWORD = re.compile(r"(?P<head>\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/:@]+:)[^\s/@]+@")
_KEY_VALUE = re.compile(
    r"(?P<head>\b(?:password|passwd|pwd|token|access_token|refresh_token|api_key|apikey|secret|client_secret)"
    r"[\"']?\s*[=:]\s*[\"']?)[^\s&\"',;}]+",
    re.IGNORECASE,
)
_MASK = "***"


def redact(text: Any) -> Any:
    """Mask passwords, tokens and keys in a log line or error message."""
    if not isinstance(text, str) or not text:
        return text
    text = _URL_PASSWORD.sub(lambda m: f"{m.group('head')}{_MASK}@", text)
    return _KEY_VALUE.sub(lambda m: f"{m.group('head')}{_MASK}", text)


class RedactFilter(logging.Filter):
    """Masks secrets in a record's message and arguments before any handler writes it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact(v) for k, v in record.args.items()}
        return True


class RedactingFormatter(logging.Formatter):
    """Also masks secrets in formatted tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging() -> None:
    """Configure the root logger: stdout always, plus a rotating logs/app.log."""
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    fmt = RedactingFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redact_filter = RedactFilter()

    root = logging.getLogger()
    # Calling this twice (tests, reloads) must not print every line twice.
    for old in [h for h in root.handlers if getattr(h, "_vision_handler", False)]:
        root.removeHandler(old)
        old.close()

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if settings.LOG_TO_FILE:
        log_dir = Path(settings.LOG_DIR)
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(
            log_dir / "app.log",
            maxBytes=max(1024 * 1024, settings.LOG_FILE_MAX_BYTES),
            backupCount=max(1, settings.LOG_FILE_BACKUP_COUNT),
            encoding="utf-8",
        ))
    for handler in handlers:
        handler.setFormatter(fmt)
        handler.addFilter(redact_filter)
        handler._vision_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(log_level)

    # uvicorn writes through its own handlers (access log lines carry the full
    # request path); mask secrets there too.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            if not any(isinstance(f, RedactFilter) for f in handler.filters):
                handler.addFilter(redact_filter)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class LogThrottle:
    """Reports a repeating problem at most once per interval, with a count of the repeats.

    A camera that is down or a model that fails on every frame would otherwise
    write the same line many times a second.
    """

    def __init__(self, interval: float = 60.0):
        self.interval = interval
        self._lock = threading.Lock()
        self._state: Dict[Hashable, Tuple[float, int]] = {}

    def log(self, logger: logging.Logger, level: int, key: Hashable, msg: str, *args: Any, **kwargs: Any) -> bool:
        """Log unless the same key was logged within the interval. Returns True if it logged."""
        now = time.monotonic()
        with self._lock:
            last, repeats = self._state.get(key, (None, 0))
            if last is not None and now - last < self.interval:
                self._state[key] = (last, repeats + 1)
                return False
            self._state[key] = (now, 0)
        if repeats:
            msg = f"{msg} (repeated {repeats} more time(s) since the last report)"
        logger.log(level, msg, *args, **kwargs)
        return True

    def clear(self, key: Hashable) -> bool:
        """Forget a key once the problem is gone. Returns True if it had been reported."""
        with self._lock:
            return self._state.pop(key, None) is not None
