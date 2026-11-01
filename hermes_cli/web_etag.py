"""Conditional GET (ETag / 304) and chunk deltas for the dashboard's JSON API.

The Desktop re-polls the same JSON (session transcript tails, the sidebar, cron
jobs, usage) every few seconds. Over a remote SSH tunnel that re-sent ~90 MB/hour
of mostly identical bodies.

* Unchanged body: the client revalidates with ``If-None-Match`` and gets an empty
  304.
* Changed body, client opted in with ``X-Hermes-Delta: 1`` and the server still
  holds the base the client offered: the response is a chunk delta against that
  base (runs of unchanged chunks plus new text), tagged ``X-Hermes-Delta-Base``.
  The client rebuilds the bytes and checks them against the new ETag (a SHA-256
  prefix both sides can compute), falling back to a full fetch on mismatch.

Pure ASGI (not ``BaseHTTPMiddleware``) so streaming responses (SSE, file
downloads) pass through untouched: only a 200 ``application/json`` response that
declares a ``content-length`` up to ``_MAX_BUFFER_BYTES`` is buffered. Registered
before the auth middlewares, so it runs inside them.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict

_MAX_BUFFER_BYTES = 8 * 1024 * 1024
# Deltas only pay off on large bodies; keep the base store small and bounded.
_DELTA_MIN_BYTES = 16 * 1024
_DELTA_STORE_ENTRIES = 64
_DELTA_STORE_MAX_BYTES = 32 * 1024 * 1024
# A delta larger than this share of the full body is not worth the client work.
_DELTA_MAX_RATIO = 0.6
# Chunk at every JSON object whose first key is "id" (session rows, transcript
# messages), so one changed or moved row costs one chunk.
_CHUNK_BOUNDARY = re.compile(rb'(?=\{"id":)')


def _etag_for(body: bytes) -> str:
    return 'W/"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def _chunks(body: bytes) -> list[bytes]:
    return [c for c in _CHUNK_BOUNDARY.split(body) if c]


def build_delta(base: bytes, new: bytes) -> list:
    """Ops rebuilding ``new`` from ``base`` chunks: ``[start, count]`` copies a run of base
    chunks, a string inserts literal text."""
    index: dict[bytes, int] = {}
    for i, chunk in enumerate(_chunks(base)):
        index.setdefault(chunk, i)
    ops: list = []
    for chunk in _chunks(new):
        at = index.get(chunk)
        if at is None:
            if ops and isinstance(ops[-1], str):
                ops[-1] += chunk.decode("utf-8")
            else:
                ops.append(chunk.decode("utf-8"))
        elif ops and isinstance(ops[-1], list) and ops[-1][0] + ops[-1][1] == at:
            ops[-1][1] += 1
        else:
            ops.append([at, 1])
    return ops


def apply_delta(base: bytes, ops: list) -> bytes:
    """Inverse of :func:`build_delta` (the Electron client mirrors this)."""
    chunks = _chunks(base)
    out = bytearray()
    for op in ops:
        if isinstance(op, str):
            out += op.encode("utf-8")
        else:
            for chunk in chunks[op[0]:op[0] + op[1]]:
                out += chunk
    return bytes(out)


class _BaseStore:
    """Recent large bodies keyed by (request target, etag); LRU bounded by count and bytes."""

    def __init__(self):
        self._items: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, target: str, etag: str) -> bytes | None:
        with self._lock:
            body = self._items.get((target, etag))
            if body is not None:
                self._items.move_to_end((target, etag))
            return body

    def put(self, target: str, etag: str, body: bytes) -> None:
        key = (target, etag)
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return
            self._items[key] = body
            self._bytes += len(body)
            while self._items and (len(self._items) > _DELTA_STORE_ENTRIES or self._bytes > _DELTA_STORE_MAX_BYTES):
                _, old = self._items.popitem(last=False)
                self._bytes -= len(old)


def _header(headers: list, name: bytes) -> str | None:
    for key, value in headers:
        if key == name:
            return value.decode("latin-1")
    return None


def _tags(value: str | None) -> list[str]:
    return [tag.strip() for tag in (value or "").split(",") if tag.strip()]


def _bare(tag: str) -> str:
    return tag.removeprefix("W/")


class JsonETagMiddleware:
    def __init__(self, app):
        self.app = app
        self.bases = _BaseStore()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "GET" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return

        req_headers = scope.get("headers") or []
        offered = _tags(_header(req_headers, b"if-none-match"))
        wants_delta = _header(req_headers, b"x-hermes-delta") == "1"
        target = scope["path"] + "?" + scope.get("query_string", b"").decode("latin-1")
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
            await self._finish(send, start, b"".join(chunks), offered, wants_delta, target)

        await self.app(scope, receive, wrapped_send)

    async def _finish(self, send, start, body, offered, wants_delta, target):
        etag = _etag_for(body)
        headers = [(k, v) for k, v in start.get("headers", []) if k != b"content-length"]
        headers += [(b"etag", etag.encode("latin-1")), (b"cache-control", b"private, no-cache")]
        if any(_bare(tag) == _bare(etag) or tag == "*" for tag in offered):
            await send({"type": "http.response.start", "status": 304, "headers": headers})
            await send({"type": "http.response.body", "body": b""})
            return

        large = len(body) >= _DELTA_MIN_BYTES
        if large:
            self.bases.put(target, etag, body)
        if large and wants_delta:
            for tag in offered:
                base = self.bases.get(target, tag)
                if base is None:
                    continue
                payload = json.dumps(build_delta(base, body), ensure_ascii=False, separators=(",", ":")).encode()
                if len(payload) <= len(body) * _DELTA_MAX_RATIO:
                    headers += [(b"x-hermes-delta-base", tag.encode("latin-1")),
                                (b"content-length", str(len(payload)).encode("latin-1"))]
                    await send({**start, "headers": headers})
                    await send({"type": "http.response.body", "body": payload})
                    return
                break

        headers.append((b"content-length", str(len(body)).encode("latin-1")))
        await send({**start, "headers": headers})
        await send({"type": "http.response.body", "body": body})
