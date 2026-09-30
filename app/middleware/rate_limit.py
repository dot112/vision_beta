from __future__ import annotations

import json
import math
import time
from typing import Dict, Tuple

from starlette.types import ASGIApp, Receive, Scope, Send


class RateLimitMiddleware:
    """Per-client request rate limit for API paths (token bucket per client address).

    One worker serves every request, so a single client flooding the API (a
    script in a tight loop, a stuck retry) slowed every dashboard and took model
    time from live counting. Each client address may send `rate` requests per
    second on average, in bursts of up to `burst`; past that it gets 429 with a
    Retry-After header. A long-lived stream counts once, when it opens.
    Production triggers (photo-eye or PLC) get their own bucket per address,
    so other traffic from the same machine cannot use up their budget.
    The key is the connecting address: behind a reverse proxy, uvicorn must
    trust the proxy's X-Forwarded-For (FORWARDED_ALLOW_IPS), or every user
    shares one bucket.
    Plain ASGI, like the other middleware, so streams pass straight through.
    """

    _SEPARATE_BUCKET_PREFIXES = ("/api/v1/control/trigger/",)

    _MAX_CLIENTS = 10_000

    def __init__(self, app: ASGIApp, rate: float, burst: int, path_prefix: str = "/api/") -> None:
        self.app = app
        self.rate = float(rate)
        self.burst = float(max(1, burst))
        self.path_prefix = path_prefix
        # client address -> (tokens left, monotonic time of last update)
        self._buckets: Dict[str, Tuple[float, float]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.rate <= 0 or not scope["path"].startswith(self.path_prefix):
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        key = client[0] if client else "unknown"
        if scope["path"].startswith(self._SEPARATE_BUCKET_PREFIXES):
            key += "|trigger"
        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self._MAX_CLIENTS:
                self._prune(now)
            tokens = self.burst
        else:
            tokens = min(self.burst, bucket[0] + (now - bucket[1]) * self.rate)

        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            await self._reject(send, retry_after=max(1, math.ceil((1.0 - tokens) / self.rate)))
            return

        self._buckets[key] = (tokens - 1.0, now)
        await self.app(scope, receive, send)

    def _prune(self, now: float) -> None:
        """Forget clients whose bucket has refilled; if none have, the longest idle ones."""
        refill_seconds = self.burst / self.rate
        for key in [k for k, (_, last) in self._buckets.items() if now - last >= refill_seconds]:
            del self._buckets[key]
        if len(self._buckets) >= self._MAX_CLIENTS:
            oldest = sorted(self._buckets, key=lambda k: self._buckets[k][1])[: len(self._buckets) // 10 or 1]
            for key in oldest:
                del self._buckets[key]

    @staticmethod
    async def _reject(send: Send, retry_after: int) -> None:
        body = json.dumps({"detail": "Too many requests from this client; slow down and retry"}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"retry-after", str(retry_after).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})
