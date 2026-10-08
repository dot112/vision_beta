from __future__ import annotations

from typing import Awaitable, Callable


class _BodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Bound multipart request bodies before Starlette spools uploads to disk.

    ``limits`` maps a path to its largest body. A key ending in ``/*`` covers
    every path below it (an upload address that carries an id).
    """

    def __init__(self, app, limits: dict[str, int]):
        self.app = app
        self.limits = {path: size for path, size in limits.items() if not path.endswith("/*")}
        self.prefix_limits = {path[:-1]: size for path, size in limits.items() if path.endswith("/*")}

    def _limit_for(self, path: str):
        if path in self.limits:
            return self.limits[path]
        for prefix, size in self.prefix_limits.items():
            if path.startswith(prefix):
                return size
        return None

    async def __call__(self, scope, receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]]):
        limit = self._limit_for(scope.get("path") or "") if scope["type"] == "http" else None
        if limit is None:
            await self.app(scope, receive, send)
            return

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
