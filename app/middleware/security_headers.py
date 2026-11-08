from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
)

# Scripts, styles, fonts and requests come from this server only, so a script
# that was injected somehow can neither load code from elsewhere nor send a
# token away. The dashboard's own code is inline, hence 'unsafe-inline'.
CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "media-src 'self' blob:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
))
# FastAPI's /docs and /redoc pages load Swagger UI and ReDoc from a CDN (only with DEBUG on).
_NO_POLICY_PATHS = ("/docs", "/docs/oauth2-redirect", "/redoc")


class SecurityHeadersMiddleware:
    """Add browser security headers without constraining the dashboard's inline UI.

    Plain ASGI rather than BaseHTTPMiddleware, so streamed responses are not
    buffered through an extra task and memory stream.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS:
                    headers.setdefault(name, value)
                if scope["path"] not in _NO_POLICY_PATHS:
                    headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
            await send(message)

        await self.app(scope, receive, send_with_headers)
