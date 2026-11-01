"""Per-project deltas for ``projects.tree`` re-reads.

The Desktop re-reads the project tree on every ``sessions.changed``. While a chat is
working, one project's counts / ``lastActive`` / previews move on almost every read,
so a whole-tree digest rarely matches and the full tree (~50 KB) was re-sent every
few seconds over remote links.

Each project node gets a content hash. A client that holds a tree (``known_digest``)
and opts in (``accept_delta``) receives the new node order as hashes plus only the
nodes it does not already have. The server remembers just the hash lists of recent
trees (no bodies), so memory stays tiny and bounded.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from typing import Any

_MAX_TREES = 64

_lock = threading.Lock()
_known: OrderedDict[str, tuple[frozenset[str], str]] = OrderedDict()


def _hash(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _remember(digest: str, order: list[str], scoped_hash: str) -> None:
    with _lock:
        _known[digest] = (frozenset(order), scoped_hash)
        _known.move_to_end(digest)
        while len(_known) > _MAX_TREES:
            _known.popitem(last=False)


def _lookup(digest: str) -> tuple[frozenset[str], str] | None:
    with _lock:
        hit = _known.get(digest)
        if hit is not None:
            _known.move_to_end(digest)
        return hit


def tree_reply(body: dict, known_digest: str | None, accept_delta: bool) -> dict:
    """The ``projects.tree`` result for ``body`` given what the client says it holds."""
    projects = body["projects"]
    order = [_hash(node) for node in projects]
    scoped_hash = _hash(body["scoped_session_ids"])
    digest = _hash({"order": order, "active": body["active_id"], "scoped": scoped_hash})

    if known_digest == digest:
        return {"projects": [], "unchanged": True, "digest": digest}

    base = _lookup(known_digest) if (accept_delta and known_digest) else None
    _remember(digest, order, scoped_hash)

    if base is None:
        return {**body, "digest": digest, "order": order}

    held, base_scoped_hash = base
    reply: dict = {
        "projects": [],
        "active_id": body["active_id"],
        "digest": digest,
        "base_digest": known_digest,
        "order": order,
        "nodes": {h: node for h, node in zip(order, projects) if h not in held},
    }
    if scoped_hash == base_scoped_hash:
        reply["scoped_unchanged"] = True
    else:
        reply["scoped_session_ids"] = body["scoped_session_ids"]
    return reply
