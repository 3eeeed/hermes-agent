"""A stale pool snapshot must never write a spent OAuth pair over a newer rotation.

Refresh tokens are single-use: when one process rotates a row, the pair another
long-lived process loaded earlier is revoked upstream. ``write_credential_pool``
used to persist that process's whole in-memory snapshot on any unrelated
mutation, resurrecting the revoked pair so every process then hit
``invalid_grant`` and the account looked signed out.
"""

import time

from agent.credential_pool import load_pool
from hermes_cli.auth import _load_auth_store, write_credential_pool


def _row(cid, label, access, refresh, priority, expires_s):
    return {
        "id": cid, "label": label, "auth_type": "oauth", "priority": priority,
        "source": "manual:hermes_pkce", "access_token": access,
        "refresh_token": refresh, "expires_at_ms": int(expires_s * 1000),
    }


def _disk(cid):
    return next(r for r in _load_auth_store()["credential_pool"]["anthropic"] if r["id"] == cid)


def test_stale_snapshot_does_not_resurrect_rotated_pair():
    now = time.time()
    write_credential_pool("anthropic", [
        _row("aaa", "one", "AT1", "RT1", 0, now + 60),
        _row("bbb", "two", "BT1", "BRT1", 1, now + 3600),
    ])
    stale = load_pool("anthropic")
    fresh = load_pool("anthropic")

    target = next(e for e in fresh._entries if e.id == "aaa")
    fresh._adopt(target, access_token="AT2", refresh_token="RT2",
                 expires_at_ms=int((now + 8 * 3600) * 1000))

    # The stale process mutates a DIFFERENT row, which rewrites the whole pool.
    stale._mark_exhausted(next(e for e in stale._entries if e.id == "bbb"), 429)

    assert _disk("aaa")["refresh_token"] == "RT2"
    assert _disk("aaa")["access_token"] == "AT2"
    assert _disk("bbb")["last_status"] == "exhausted"


def test_newer_in_memory_rotation_still_wins():
    now = time.time()
    write_credential_pool("anthropic", [_row("aaa", "one", "AT1", "RT1", 0, now + 60)])
    pool = load_pool("anthropic")
    entry = next(e for e in pool._entries if e.id == "aaa")
    pool._adopt(entry, access_token="AT2", refresh_token="RT2",
                expires_at_ms=int((now + 8 * 3600) * 1000))
    assert _disk("aaa")["refresh_token"] == "RT2"
