"""Switching the pinned pool account (Claude or Codex) must compact the history before dispatch.

A long transcript sent to a freshly selected account is written to that account's prompt cache in
full — several hundred thousand tokens at full price — and the very next turn pays for it again
until the cache warms. Compacting first makes the switch cost one summary instead.
"""
import contextlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tui_gateway import server


SERVED = "served_pool_credential"


class _DB:
    """model_config store: the pin key answers ``pinned``, the served key is real storage."""

    def __init__(self, pinned):
        self.pinned = pinned
        self.values = {}
        self.pin_reads = []

    def get_session_model_config_value(self, session_id, key):
        if key == SERVED:
            return self.values.get(SERVED)
        self.pin_reads.append((session_id, key))
        return self.pinned

    def patch_session_model_config(self, session_id, patch):
        self.values.update(patch)


@pytest.fixture
def wired(monkeypatch):
    entries = [SimpleNamespace(id="first"), SimpleNamespace(id="second")]
    db = _DB(pinned="first")
    agent = SimpleNamespace(
        provider="anthropic", session_id="stored-session",
        _credential_pool=SimpleNamespace(entries=lambda: entries, provider="anthropic"),
        _swap_credential=Mock(return_value=True),
    )
    history = [{"role": "user", "content": f"m{i}"} for i in range(12)]
    session = {"agent": agent, "session_key": "stored-session", "history": history,
               "history_version": 3, "history_lock": contextlib.nullcontext()}
    compress = Mock(return_value=(8, {}))
    sync = Mock()
    monkeypatch.setattr(server, "_session_db", lambda s: contextlib.nullcontext(db))
    monkeypatch.setattr(server, "_compress_session_history", compress)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", sync)
    monkeypatch.setattr(server, "_status_update", Mock())
    monkeypatch.setattr(server, "_emit", Mock())
    monkeypatch.setattr(server, "_session_info", Mock(return_value={}))
    return session, agent, db, compress, sync


def _turn(session, agent):
    """One turn's account wiring in production order: apply the pin, then compact on change."""
    server._apply_session_credential_selection(session, agent)
    server._compact_on_account_change("rt", session, agent)


def test_first_turn_records_account_without_compacting(wired):
    session, agent, db, compress, _sync = wired
    _turn(session, agent)
    compress.assert_not_called()
    assert session["active_pool_credential"] == "anthropic:first"
    assert db.values[SERVED] == "anthropic:first"


def test_account_change_compacts_before_dispatch(wired):
    session, agent, db, compress, sync = wired
    _turn(session, agent)
    db.pinned = "second"
    _turn(session, agent)
    compress.assert_called_once()
    sync.assert_called_once()
    assert sync.call_args.kwargs.get("clear_pending_title") is False
    assert session["active_pool_credential"] == "anthropic:second"
    assert db.values[SERVED] == "anthropic:second"


def test_change_detected_after_backend_restart(wired):
    """The served account is read back from the DB when the live session has no memory of it."""
    session, agent, db, compress, _sync = wired
    db.values[SERVED] = "anthropic:second"  # previous process served the other account
    _turn(session, agent)  # pinned is "first" now
    compress.assert_called_once()


def test_same_account_does_not_compact(wired):
    session, agent, db, compress, _sync = wired
    _turn(session, agent)
    _turn(session, agent)
    compress.assert_not_called()


def test_compaction_failure_does_not_block_the_turn(wired):
    session, agent, db, compress, _sync = wired
    _turn(session, agent)
    db.pinned = "second"
    compress.side_effect = RuntimeError("boom")
    _turn(session, agent)  # must not raise


def test_codex_pool_is_covered_too(wired):
    session, agent, db, compress, _sync = wired
    agent.provider = "openai-codex"
    agent._credential_pool.provider = "openai-codex"
    _turn(session, agent)
    assert db.pin_reads[-1] == ("stored-session", "openai_codex_credential_id")
    db.pinned = "second"
    _turn(session, agent)
    compress.assert_called_once()
