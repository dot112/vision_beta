from __future__ import annotations

from typing import Awaitable, Callable


class _BodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Bound multipart request bodies before Starlette spools uploads to disk."""

    def __init__(self, app, limits: dict[str, int]):
        self.app = app
        self.limits = limits

    async def __call__(self, scope, receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]]):
        if scope["type"] != "http" or scope.get("path") not in self.limits:
            await self.app(scope, receive, send)
            return

        limit = self.limits[scope["path"]]
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > limit:
                    await self._send_too_large(send)
                    return
            except ValueError:
                pass

        total = 0

        async def limited_receive() -> dict:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > limit:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await self._send_too_large(send)

    @staticmethod
    async def _send_too_large(send: Callable[[dict], Awaitable[None]]) -> None:
        body = b'{"detail":"Request body exceeds the configured upload limit"}'
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})
