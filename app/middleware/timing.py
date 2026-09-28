from __future__ import annotations

import time

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class TimingMiddleware:
    """Adds X-Process-Time header to every response.

    Plain ASGI rather than BaseHTTPMiddleware, so response bodies (including
    long-running MJPEG streams) pass straight through without an extra task
    and memory stream per request. The time covers the work up to the moment
    the response headers are sent.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()

        async def send_with_timing(message: Message) -> None:
            if message["type"] == "http.response.start":
                elapsed = (time.perf_counter() - start) * 1000
                MutableHeaders(scope=message)["X-Process-Time"] = f"{elapsed:.2f}ms"
            await send(message)

        await self.app(scope, receive, send_with_timing)
