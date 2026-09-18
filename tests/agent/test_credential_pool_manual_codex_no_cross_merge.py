"""Regression: refreshing one ``manual:device_code`` Codex/xAI account must
never adopt another independent account's tokens through the shared
``providers.<id>.tokens`` singleton slot.

Both accounts write their rotated pair into that shared slot on refresh
(``_sync_device_code_entry_to_auth_store``), so ``_sync_entry_from_auth_store``
previously treated ANY ``manual:device_code`` entry as a legacy singleton
alias and adopted whatever sat in the slot whenever it differed from the
entry's own tokens. Two independently-added accounts refreshing minutes apart
therefore converged onto whichever refreshed last, silently merging distinct
accounts into one (surfaced as identical access/refresh tokens and identical
``last_refresh`` timestamps across "different" pool entries).

The fix: ``_sync_entry_from_auth_store`` only ever syncs the singleton-seeded
``device_code`` entry from auth.json. ``manual:device_code`` entries are
independent credentials and refresh purely from their own stored
refresh_token, never adopting the shared slot's contents.
"""
import json

import pytest


def _seed_auth_store(tmp_path, monkeypatch, *, singleton_tokens, pool_entries):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {
            "openai-codex": {
                "tokens": singleton_tokens,
                "last_refresh": "2026-01-01T00:00:00Z",
                "auth_mode": "chatgpt",
            },
        },
        "credential_pool": {"openai-codex": pool_entries},
    }))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))


def _manual_entry(entry_id, *, access_token, refresh_token):
    return {
        "id": entry_id,
        "label": entry_id,
        "source": "manual:device_code",
        "auth_type": "oauth",
        "priority": 0,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "base_url": "https://chatgpt.com/backend-api/codex",
        "last_refresh": "2026-01-01T00:00:00Z",
    }


def test_sync_entry_from_auth_store_ignores_manual_device_code(tmp_path, monkeypatch):
    """The read-path sync must skip ``manual:device_code`` entries entirely.

    Simulates the exact merge trigger: account B refreshed and left ITS
    tokens in the shared singleton slot. Account A (a distinct
    ``manual:device_code`` entry with its own, different tokens) must NOT
    adopt B's tokens just because they differ from A's own pair.
    """
    _seed_auth_store(
        tmp_path, monkeypatch,
        singleton_tokens={"access_token": "account-b-at", "refresh_token": "account-b-rt"},
        pool_entries=[
            _manual_entry("account-a", access_token="account-a-at", refresh_token="account-a-rt"),
            _manual_entry("account-b", access_token="account-b-at", refresh_token="account-b-rt"),
        ],
    )
    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    entry_a = next(e for e in pool.entries() if e.id == "account-a")

    synced = pool._sync_entry_from_auth_store(entry_a)

    assert synced.access_token == "account-a-at"
    assert synced.refresh_token == "account-a-rt"


def test_sync_entry_from_auth_store_still_syncs_singleton_device_code(tmp_path, monkeypatch):
    """The singleton-seeded ``device_code`` entry must still pick up a fresh
    login/refresh written to ``providers.openai-codex.tokens`` — the sync
    this method exists for must keep working for the one source it owns."""
    _seed_auth_store(
        tmp_path, monkeypatch,
        singleton_tokens={"access_token": "fresh-at", "refresh_token": "fresh-rt"},
        pool_entries=[{
            "id": "seeded",
            "label": "seeded",
            "source": "device_code",
            "auth_type": "oauth",
            "priority": 0,
            "access_token": "stale-at",
            "refresh_token": "stale-rt",
            "base_url": "https://chatgpt.com/backend-api/codex",
        }],
    )
    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    entry = next(e for e in pool.entries() if e.id == "seeded")

    synced = pool._sync_entry_from_auth_store(entry)

    assert synced.access_token == "fresh-at"
    assert synced.refresh_token == "fresh-rt"
