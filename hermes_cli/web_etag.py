"""Conditional GET (ETag / 304) for the dashboard's JSON API.

The Desktop re-polls the same JSON (session transcript tails, cron jobs,
usage) every few seconds even when nothing changed. Over a remote SSH tunnel
that re-sent ~90 MB/hour of identical bodies. A weak content-hash ETag lets the
client revalidate with ``If-None-Match`` and receive an empty 304 instead.

Pure ASGI (not ``BaseHTTPMiddleware``) so streaming responses (SSE, file
downloads) pass through untouched: only a 200 ``application/json`` response
that declares a ``content-length`` up to ``_MAX_BUFFER_BYTES`` is buffered.
"""

from __future__ import annotations

import hashlib

_MAX_BUFFER_BYTES = 8 * 1024 * 1024


def _etag_for(body: bytes) -> str:
    return 'W/"' + hashlib.blake2b(body, digest_size=16).hexdigest() + '"'


def _if_none_match(headers: list) -> set[str]:
    for key, value in headers:
        if key == b"if-none-match":
            return {tag.strip().removeprefix("W/") for tag in value.decode("latin-1").split(",") if tag.strip()}
    return set()


class JsonETagMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "GET" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return

        wanted = _if_none_match(scope.get("headers") or [])
        start: dict | None = None
        chunks: list[bytes] = []
        buffering = False

        async def wrapped_send(message):
            nonlocal start, buffering
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers") or [])
                length = headers.get(b"content-length")
                buffering = (
                    message["status"] == 200
                    and headers.get(b"content-type", b"").startswith(b"application/json")
                    and b"etag" not in headers
                    and length is not None
                    and int(length) <= _MAX_BUFFER_BYTES
                )
                if not buffering:
                    await send(message)
                    return
                start = message
                return
            if message["type"] != "http.response.body" or not buffering:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = b"".join(chunks)
            etag = _etag_for(body)
            headers = [(k, v) for k, v in start.get("headers", []) if k not in (b"content-length",)]
            headers += [(b"etag", etag.encode("latin-1")), (b"cache-control", b"private, no-cache")]
            if etag.removeprefix("W/") in wanted or "*" in wanted:
                await send({"type": "http.response.start", "status": 304, "headers": headers})
                await send({"type": "http.response.body", "body": b""})
                return
            headers.append((b"content-length", str(len(body)).encode("latin-1")))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, wrapped_send)
