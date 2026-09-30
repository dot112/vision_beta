"""Container entrypoint: runs the server and restarts the container when it hangs.

Docker's restart policy only acts when the main process exits; a container
that is running but no longer answering stays "unhealthy" forever. This
supervisor runs uvicorn as a child, polls the public /health endpoint and,
after WATCHDOG_FAILURES failed checks in a row, stops the server and exits
with status 1 so `restart: unless-stopped` starts a fresh container.

It also forwards SIGTERM/SIGINT to uvicorn and waits for it, so `docker stop`
and host shutdowns run the application's own shutdown: open streams close,
queued PLC operations finish and PLC outputs go to their safe state.

Settings (environment):
  PORT                       port uvicorn listens on (default 8000)
  GRACEFUL_SHUTDOWN_SECONDS  time uvicorn gives open requests/streams (default 20)
  WATCHDOG_ENABLED           "0" turns the health watchdog off (default on)
  WATCHDOG_START_PERIOD      seconds before the first check (default 90)
  WATCHDOG_INTERVAL          seconds between checks (default 15)
  WATCHDOG_TIMEOUT           seconds a check may take (default 5)
  WATCHDOG_FAILURES          failed checks in a row before a restart (default 4)
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


PORT = int(_env_float("PORT", 8000))
GRACE = _env_float("GRACEFUL_SHUTDOWN_SECONDS", 20)
WATCHDOG = os.environ.get("WATCHDOG_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
START_PERIOD = _env_float("WATCHDOG_START_PERIOD", 90)
INTERVAL = max(1.0, _env_float("WATCHDOG_INTERVAL", 15))
TIMEOUT = max(1.0, _env_float("WATCHDOG_TIMEOUT", 5))
FAILURES = max(1, int(_env_float("WATCHDOG_FAILURES", 4)))
# The app's own shutdown (PLC safe states, driver close) runs after uvicorn's
# grace period; allow it this long before the server is killed.
SHUTDOWN_EXTRA = 20.0

stopping = threading.Event()
# Why the server is being stopped: "signal" (docker stop, host shutdown) or "watchdog".
stop_reason: list[str] = []


def log(message: str) -> None:
    print(f"[supervisor] {message}", file=sys.stderr, flush=True)


def healthy() -> tuple[bool, str]:
    """The server answers /health with 200 (a degraded plant is still 200; a dead database is 503)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=TIMEOUT) as res:
            return res.status == 200, f"HTTP {res.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # refused, timed out, reset
        return False, f"{type(exc).__name__}: {exc}"


def stop_child(child: subprocess.Popen, reason: str) -> None:
    if child.poll() is not None:
        return
    log(f"stopping the server ({reason})")
    child.send_signal(signal.SIGTERM)
    try:
        child.wait(timeout=GRACE + SHUTDOWN_EXTRA)
    except subprocess.TimeoutExpired:
        log("the server did not stop in time; killing it")
        child.kill()
        child.wait()


def request_stop(child: subprocess.Popen, reason: str) -> bool:
    """Start stopping the server once; returns False if a stop is already under way."""
    if stopping.is_set():
        return False
    stop_reason.append(reason)
    stopping.set()
    threading.Thread(target=stop_child, args=(child, reason), daemon=True).start()
    return True


def watchdog(child: subprocess.Popen) -> None:
    if stopping.wait(START_PERIOD):
        return
    failed = 0
    while not stopping.is_set() and child.poll() is None:
        ok, detail = healthy()
        if ok:
            if failed:
                log(f"health check recovered after {failed} failure(s)")
            failed = 0
        else:
            failed += 1
            log(f"health check failed ({failed}/{FAILURES}): {detail}")
            if failed >= FAILURES:
                if not stopping.is_set():
                    log("the server stopped answering; restarting the container")
                    request_stop(child, "watchdog")
                return
        stopping.wait(INTERVAL)


def main() -> int:
    cmd = [
        sys.executable, "-m", "uvicorn", "main:app",
        "--host", "0.0.0.0",
        "--port", str(PORT),
        # One worker: camera connections and PLC drivers live in process memory.
        "--workers", "1",
        "--timeout-graceful-shutdown", str(int(GRACE)),
    ]
    child = subprocess.Popen(cmd)

    def on_signal(signum, _frame) -> None:
        request_stop(child, signal.Signals(signum).name)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    if WATCHDOG:
        threading.Thread(target=watchdog, args=(child,), name="watchdog", daemon=True).start()
    else:
        log("health watchdog is off (WATCHDOG_ENABLED=0)")

    code = child.wait()
    reason = stop_reason[0] if stop_reason else None
    if reason == "watchdog":
        # Non-zero, so the restart policy brings up a fresh container.
        return 1
    if reason:
        log(f"server stopped after {reason} (exit status {code})")
        return 0 if code in (0, -signal.SIGTERM, -signal.SIGINT) else code
    log(f"server exited unexpectedly (exit status {code})")
    return code if code else 1


if __name__ == "__main__":
    sys.exit(main())
