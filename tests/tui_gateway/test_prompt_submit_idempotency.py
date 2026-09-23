"""``prompt.submit`` accepted once per client send id, however many times it arrives.

A "Move here" resend is issued over a socket that has already proved it can drop:
the chat was refused because another device owned it, and the claim + resend ride
the same WebSocket. If the acceptance reaches the gateway and the RESPONSE is
lost, the client cannot tell that from a request that never landed — and a retry
would post the user's message twice and run two turns on one chat.

The fix is end-to-end: the client mints one immutable send id per user message
and reuses it for every retry of THAT message, and the gateway answers a repeat
with the acceptance it already gave instead of starting a second turn. Any other
send (a genuinely new message) carries a different id and is unaffected.
"""

from __future__ import annotations

import threading
import types

import pytest

from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport


@pytest.fixture(autouse=True)
def _quiet_gateway(monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a, **k: True)
    monkeypatch.setattr(server, "_persist_session_row_for_submit", lambda *a, **k: None)


@pytest.fixture
def session(monkeypatch):
    record = {
        "agent": types.SimpleNamespace(session_id="stored-1", clear_interrupt=lambda: None),
        "session_key": "stored-1",
        "_sid": "runtime-1",
        "history": [],
        "history_lock": threading.RLock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "cols": 80,
        "source": "desktop",
        "slash_worker": None,
        "inflight_turn": None,
        "active_session_lease": None,
    }
    with server._sessions_lock:
        server._sessions["runtime-1"] = record
    yield record
    with server._sessions_lock:
        server._sessions.clear()


@pytest.fixture
def turns(monkeypatch):
    """Every turn this backend actually starts."""
    started: list[str] = []

    def fake_thread(target=None, **_kwargs):
        return types.SimpleNamespace(start=lambda: None, is_alive=lambda: False, join=lambda timeout=None: None)

    monkeypatch.setattr(server, "_run_after_agent_ready", lambda *a, **k: None)
    monkeypatch.setattr(
        server, "_lock_in_submit_turn",
        lambda rid, sid, sess, text, *a, **k: (started.append(text), (None, {}))[1])
    monkeypatch.setattr(threading, "Thread", fake_thread)
    return started


def _submit(params: dict) -> dict:
    token = bind_transport(None)
    try:
        return server.handle_request({"id": 1, "method": "prompt.submit", "params": params})
    finally:
        reset_transport(token)


def test_a_replayed_send_id_is_accepted_once_and_runs_one_turn(session, turns):
    """The response-loss ordering: accepted, reply lost, client retries.

    Both calls must be answered as accepted — the client is entitled to a
    definite answer — but exactly one turn may begin and exactly one user
    message may be submitted.
    """
    params = {"session_id": "runtime-1", "text": "look at this", "client_send_id": "send-abc"}

    first = _submit(dict(params))
    second = _submit(dict(params))

    assert first.get("error") is None, first
    assert second.get("error") is None, second
    assert second["result"] == first["result"], (
        "a retry must be answered with the acceptance the gateway already gave"
    )
    assert turns == ["look at this"], f"exactly one turn may begin: {turns}"


def test_a_different_send_id_is_a_different_message(session, turns):
    """Dedup must not swallow the next thing the user says."""
    _submit({"session_id": "runtime-1", "text": "first", "client_send_id": "send-1"})
    _submit({"session_id": "runtime-1", "text": "second", "client_send_id": "send-2"})

    assert turns == ["first", "second"]


def test_a_send_without_an_id_is_never_deduplicated(session, turns):
    """Older clients (and every internal submit path) keep their exact behaviour."""
    _submit({"session_id": "runtime-1", "text": "same words"})
    _submit({"session_id": "runtime-1", "text": "same words"})

    assert turns == ["same words", "same words"]


def test_the_accepted_send_id_memory_is_bounded(session, turns):
    """A long conversation must not accumulate one record per message forever."""
    for index in range(server._ACCEPTED_SEND_ID_LIMIT + 25):
        _submit({"session_id": "runtime-1", "text": f"m{index}", "client_send_id": f"send-{index}"})

    assert len(session["_accepted_send_ids"]) <= server._ACCEPTED_SEND_ID_LIMIT
    # The most recent ids are the ones a retry could still arrive for.
    assert f"send-{server._ACCEPTED_SEND_ID_LIMIT + 24}" in session["_accepted_send_ids"]
