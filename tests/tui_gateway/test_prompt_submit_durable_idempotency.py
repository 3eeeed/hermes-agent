"""``prompt.submit`` is exactly-once against the DURABLE session, not against a live dict.

The process-local twin of this file (``test_prompt_submit_idempotency.py``) pins the
easy half: one gateway process, one live session record, one retry. Every ordering the
"Move here" resend actually rides is outside that reach:

* two requests for the same send id **in flight at once** — check-then-remember has a
  window between them, and both sides win it;
* an acceptance whose RESPONSE was lost, retried after the chat **resumed under a new
  runtime id** (or a new backend process) — the remembering dict died with the old one;
* the same send id carrying a **different message** — a client bug or a crossed retry
  must fail closed, not overwrite or double-post;
* a send that was **refused** (the fence, a storage failure) — that id must stay usable,
  or the user's message is unsendable forever;
* a send whose durable row landed but whose turn never started — the retry has to run
  the turn **without writing the user's message twice**.

So the record of "this message was accepted" has to live where the session itself lives:
the profile's ``state.db``, written in the same transactional store as the transcript it
protects, and consulted before anything is mutated.
"""

from __future__ import annotations

import threading
import types

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport


class _Gateway:
    """One desktop chat on a real ``state.db``, submittable like a client."""

    def __init__(self, db: SessionDB, sid: str, key: str, turns: list, refusals: list):
        self.db, self.sid, self.key, self.turns, self.refusals = db, sid, key, turns, refusals

    def submit(self, **params) -> dict:
        token = bind_transport(None)
        try:
            return server.handle_request(
                {"id": 1, "method": "prompt.submit", "params": {"session_id": self.sid, **params}})
        finally:
            reset_transport(token)

    def user_rows(self) -> list:
        """Every durable user message of the STORED session, live or archived."""
        return [
            row.get("content")
            for row in self.db.get_messages_as_conversation(self.key, include_inactive=True)
            if row.get("role") == "user"]

    def resume_under_new_runtime(self, new_sid: str = "runtime-2") -> dict:
        """What a reconnect / a second backend process gives the same stored chat: a fresh
        runtime id and a session record that remembers nothing about earlier sends."""
        with server._sessions_lock:
            previous = server._sessions.pop(self.sid)
            resumed = {
                **previous, "_sid": new_sid, "history": [], "history_lock": threading.RLock(),
                "running": False, "inflight_turn": None}
            for process_local in ("_accepted_send_ids", "_submit_user_row", "_run_thread"):
                resumed.pop(process_local, None)
            server._sessions[new_sid] = resumed
        self.sid = new_sid
        return resumed


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    """A desktop session whose STORE is real and whose TURN is recorded, not run.

    The durable user row, the stored session identity and the profile-scoped store are
    exactly what the ledger has to protect, so none of them is faked. What a turn does
    after it is admitted belongs to other tests.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    for stub in ("_schedule_agent_build", "_start_agent_build", "_register_session_cwd"):
        monkeypatch.setattr(server, stub, lambda *a, **k: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *a, **k: False)
    monkeypatch.setattr(server, "_run_after_agent_ready", lambda *a, **k: None)
    # Every turn this backend admits. Recorded at the one choke point a submit must pass
    # to mutate history, queue, running state or the transcript.
    turns: list = []
    monkeypatch.setattr(
        server, "_lock_in_submit_turn",
        lambda rid, sid, session, text, *a, **k: (turns.append(text), (None, {}))[1])
    # The ownership fence, scripted: a queued entry is the refusal the next submit gets.
    refusals: list = []
    monkeypatch.setattr(
        server, "_ensure_active_session_slot",
        lambda sid, session: refusals.pop(0) if refusals else None)

    created = server.handle_request(
        {"id": "c", "method": "session.create", "params": {"cols": 96, "source": "desktop"}})
    assert "result" in created, created
    sid, key = created["result"]["session_id"], created["result"]["stored_session_id"]
    try:
        yield _Gateway(db, sid, key, turns, refusals)
    finally:
        with server._sessions_lock:
            server._sessions.clear()
        db.close()


def test_two_concurrent_requests_for_one_send_id_post_the_message_once(gateway):
    """(a) The window between "have I seen this id?" and "remember it".

    Two sockets, one message: a retry the client fired before the first response
    arrived, or the same move replayed by two windows. Both requests are in the
    gateway at once, so neither can see the other's memory — only a durable,
    atomic reservation decides which one owns the send.
    """
    params = {"text": "ship the release notes", "client_send_id": "send-concurrent"}
    both_ready = threading.Barrier(2)
    responses: list = []
    lock = threading.Lock()

    def submit_once():
        both_ready.wait(timeout=10)
        response = gateway.submit(**params)
        with lock:
            responses.append(response)

    racers = [threading.Thread(target=submit_once) for _ in range(2)]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join(timeout=30)

    accepted = [r for r in responses if not r.get("error")]
    retryable = [r for r in responses if (r.get("error") or {}).get("code") == 4133]
    assert len(accepted) == 1, responses
    assert len(retryable) == 1, responses
    # The racing caller retained the immutable message and can now replay the
    # acceptance after the holder settled.
    assert gateway.submit(**params).get("error") is None
    assert gateway.user_rows() == ["ship the release notes"], (
        "one message, one durable user row — the loser of the race must mutate nothing")
    assert gateway.turns == ["ship the release notes"], f"exactly one turn: {gateway.turns}"


def test_a_racing_retry_keeps_its_message_when_the_holder_later_fails(gateway, monkeypatch):
    """`in_progress` is not acceptance: the holder can still fail and release it."""
    params = {"text": "do not lose this", "client_send_id": "send-racing-failure"}
    entered = threading.Event()
    let_holder_fail = threading.Event()
    real_submit = server._submit_prompt
    attempts = 0

    def controlled_submit(rid, submit_params, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            assert let_holder_fail.wait(timeout=10)
            raise RuntimeError("holder failed before acceptance")
        return real_submit(rid, submit_params, **kwargs)

    monkeypatch.setattr(server, "_submit_prompt", controlled_submit)
    first_response: list[dict] = []
    holder = threading.Thread(target=lambda: first_response.append(gateway.submit(**params)))
    holder.start()
    assert entered.wait(timeout=10)

    try:
        overtaking = gateway.submit(**params)
        assert overtaking.get("error"), "an in-progress send must not be reported accepted"
        assert overtaking["error"]["data"]["retryable"] is True
    finally:
        let_holder_fail.set()
    holder.join(timeout=30)
    assert first_response and first_response[0].get("error")

    retried = gateway.submit(**params)
    assert retried.get("error") is None, retried
    assert gateway.user_rows() == ["do not lose this"]
    assert gateway.turns == ["do not lose this"]


def test_a_failed_durable_acceptance_is_never_reported_as_success(gateway, monkeypatch):
    """Process-local memory is not an exactly-once acceptance after the DB write failed."""
    monkeypatch.setattr(gateway.db, "accept_prompt_send", lambda *_a, **_k: False)

    response = gateway.submit(text="durability first", client_send_id="send-accept-fails")

    assert response.get("error"), response
    assert response["error"]["code"] == 5074
    session = server._sessions[gateway.sid]
    assert "send-accept-fails" not in (session.get("_accepted_send_ids") or {})


def test_a_lost_acceptance_retried_on_a_new_runtime_does_not_post_twice(gateway):
    """(b) The exact "Move here" failure: accepted here, answer lost, retried there.

    The chat comes back under a new runtime id (reconnect, second device, restarted
    backend). The live record that remembered the acceptance is gone; the durable one
    must answer the retry.
    """
    params = {"text": "look at this diff", "client_send_id": "send-lost-reply"}
    accepted = gateway.submit(**params)
    assert accepted.get("error") is None, accepted

    gateway.resume_under_new_runtime()
    replayed = gateway.submit(**params)

    assert replayed.get("error") is None, replayed
    assert replayed["result"] == accepted["result"], (
        "the retry is entitled to the acceptance the gateway already gave")
    assert gateway.user_rows() == ["look at this diff"]
    assert gateway.turns == ["look at this diff"]


def test_an_accepted_image_retry_consumes_only_its_restaged_attachment(gateway):
    """A durable replay bypasses submit, but must not leak its image into the next turn."""
    path = "/tmp/retried-image.png"
    params = {
        "text": "look at this image",
        "client_send_id": "send-image-lost-reply",
    }
    accepted = gateway.submit(**params, staged_attachment_paths=[path])
    assert accepted.get("error") is None, accepted

    resumed = gateway.resume_under_new_runtime()
    # A same-path occurrence already present was not staged by this retry and is
    # therefore not the accepted send's to consume.
    resumed["attached_images"] = [path, "/tmp/newer-image.png"]
    replayed_without_stage = gateway.submit(**params)
    assert replayed_without_stage.get("error") is None, replayed_without_stage
    assert resumed["attached_images"] == [path, "/tmp/newer-image.png"]

    # When this retry really stages its own equal-path copy, staging appends it;
    # replay consumes that newest occurrence and preserves the older one.
    resumed["attached_images"].append(path)
    replayed = gateway.submit(**params, staged_attachment_paths=[path])

    assert replayed.get("error") is None, replayed
    assert resumed["attached_images"] == [path, "/tmp/newer-image.png"]
    assert gateway.user_rows() == ["look at this image"]
    assert gateway.turns == ["look at this image"]


def test_the_ledger_outlives_every_process_local_cache(gateway):
    """(c) A restarted backend has no memory at all — only the store."""
    params = {"text": "restart me", "client_send_id": "send-restart"}
    assert gateway.submit(**params).get("error") is None

    # Everything this process kept about the send, gone: the accepted-id memory, the
    # staged row, the session record itself. A fresh gateway has exactly this much.
    gateway.resume_under_new_runtime("runtime-after-restart")
    assert not any(
        "_accepted_send_ids" in session for session in server._sessions.values()), (
        "the restart simulation must leave no in-memory acceptance behind")

    retried = gateway.submit(**params)

    assert retried.get("error") is None, retried
    assert retried["result"].get("status"), "a replay must still read as accepted"
    assert gateway.user_rows() == ["restart me"]
    assert gateway.turns == ["restart me"]


def test_the_same_send_id_with_a_different_message_fails_closed(gateway):
    """(d) One id must mean one message. Reusing it for different words is a client
    bug (or two crossed retries) — answering it as "already accepted" would silently
    swallow what the user typed, and posting it would double-post under one identity."""
    assert gateway.submit(text="first words", client_send_id="send-reused").get("error") is None

    conflict = gateway.submit(text="COMPLETELY different words", client_send_id="send-reused")

    assert conflict.get("error"), "a reused send id carrying a new payload must be refused"
    assert conflict["error"]["code"] == 4134, conflict
    assert gateway.user_rows() == ["first words"], "the refused payload must mutate nothing"
    assert gateway.turns == ["first words"]


def test_a_truncation_flag_alone_makes_it_a_different_payload(gateway):
    """(d) The digest covers the whole submit, not just its text: a rewind carrying the
    same words as an ordinary send is a destructive replace, not a retry of it."""
    assert gateway.submit(text="try again", client_send_id="send-rewind").get("error") is None

    conflict = gateway.submit(
        text="try again", client_send_id="send-rewind", confirm_truncate=True,
        truncate_before_row_id=1)

    assert conflict.get("error") and conflict["error"]["code"] == 4134, conflict
    assert gateway.turns == ["try again"]


def test_a_refused_send_leaves_its_id_usable(gateway):
    """(e) Reservation lifecycle, the common half: the refusal happens BEFORE anything is
    mutated, so the id must come back clean. This is the Move-here flow itself — the fence
    refuses, the user clicks "Move here", and the very same message is resent under the
    very same id."""
    from hermes_cli.active_sessions import ActiveSessionRefusal, SESSION_NOT_OWNED

    gateway.refusals.append(
        ActiveSessionRefusal("another device is using this chat", SESSION_NOT_OWNED, None))
    params = {"text": "finish this thought", "client_send_id": "send-refused"}

    refused = gateway.submit(**params)
    assert refused.get("error") and refused["error"]["code"] == 4090, refused
    assert gateway.user_rows() == [] and gateway.turns == []

    resent = gateway.submit(**params)

    assert resent.get("error") is None, (
        "a send that was refused before any mutation must be retryable under its own id")
    assert gateway.user_rows() == ["finish this thought"]
    assert gateway.turns == ["finish this thought"]


def test_an_accepted_send_with_no_worker_is_terminal_not_reexecuted(gateway, monkeypatch):
    """Once accepted, a worker-start failure becomes a terminal runtime event.

    Returning a retryable 5073 after durable acceptance would let a reconnect run
    the same turn again even though an earlier worker may already have escaped.
    """

    class _ExhaustedProcess:
        """``threading`` with no threads left: Thread() itself raises."""

        def __getattr__(self, name):
            return getattr(threading, name)

        def Thread(self, *_args, **_kwargs):
            raise RuntimeError("can't start new thread")

    params = {"text": "run the migration", "client_send_id": "send-no-worker"}
    monkeypatch.setattr(server, "threading", _ExhaustedProcess())
    stranded = gateway.submit(**params)
    assert stranded.get("error") is None, stranded
    assert gateway.user_rows() == ["run the migration"]

    monkeypatch.setattr(server, "threading", threading)
    gateway.resume_under_new_runtime("runtime-after-strand")
    retried = gateway.submit(**params)

    assert retried.get("error") is None, retried
    assert gateway.user_rows() == ["run the migration"]
    assert gateway.turns == ["run the migration"], (
        "the accepted send is not admitted a second time after reconnect")


def test_a_busy_isolated_send_is_durable_before_it_enters_the_memory_queue(gateway):
    params = {
        "text": "run after this turn",
        "client_send_id": "send-busy-queued",
        "queued": True,
    }
    session = server._sessions[gateway.sid]
    session["running"] = True

    accepted = gateway.submit(**params)

    assert accepted.get("error") is None, accepted
    assert accepted["result"]["status"] == "queued"
    assert gateway.user_rows() == ["run after this turn"]
    queued = session.get("queued_prompt") or {}
    assert queued.get("_submit_user_row", {}).get("_row_id") is not None
    verdict = gateway.db.reserve_prompt_send(
        gateway.key, params["client_send_id"], server._send_payload_digest(
            {"session_id": gateway.sid, **params}),
        holder="submit:other:host=" + ("f" * 24) + ":pid=1",
    )
    assert verdict.outcome == "accepted" and verdict.user_row_id is not None

    # Process-local queue lost on crash: the transcript proof survives and an
    # unchanged retry never appends a second row.
    gateway.resume_under_new_runtime("runtime-after-queue-loss")
    resumed = server._sessions[gateway.sid]
    resumed["running"] = False
    resumed.pop("queued_prompt", None)
    resumed.pop("queued_prompts", None)
    replayed = gateway.submit(**params)

    assert replayed.get("error") is None, replayed
    assert gateway.user_rows() == ["run after this turn"]


def test_a_send_that_crashes_before_its_turn_is_retryable_at_once(gateway, monkeypatch):
    """(e) Reservation lifecycle, the unhandled half: the claim is taken BEFORE anything is
    mutated, so a bug that throws between the reservation and an answer leaves this
    process's row ``in_progress`` under a holder that is provably doing nothing. Nobody
    releases it, and this process is alive, so for the whole reservation TTL every retry is
    told "streaming" for a turn that never ran — the message the client preserved for
    exactly this case, silently dropped. The claim has to come back on the way out."""
    params = {"text": "the message that must not vanish", "client_send_id": "send-crash"}
    real_submit = server._submit_prompt
    attempts: list = []

    def _explode_on_the_first_attempt(rid, submit_params, **kwargs):
        attempts.append(submit_params.get("text"))
        if len(attempts) == 1:
            raise RuntimeError("unexpected failure inside the submit pipeline")
        return real_submit(rid, submit_params, **kwargs)

    monkeypatch.setattr(server, "_submit_prompt", _explode_on_the_first_attempt)

    crashed = gateway.submit(**params)

    assert crashed.get("error"), "an unexpected failure must come back as a terminal error"
    assert "RuntimeError" not in str(crashed["error"].get("message") or ""), (
        "the client is told to send it again, not shown the traceback's exception class")
    assert gateway.user_rows() == [] and gateway.turns == []
    assert gateway.db.prompt_send_ledger_entry(gateway.key, "send-crash") is None, (
        "nothing durable landed, so the crashed reservation must be gone — not left "
        "in_progress for a live holder that will never finish it")

    retried = gateway.submit(**params)

    assert retried.get("error") is None, (
        "the crashed send mutated nothing, so its id must be usable immediately")
    assert gateway.turns == ["the message that must not vanish"], (
        "the retry must actually run the turn the first attempt never started")
    assert gateway.user_rows() == ["the message that must not vanish"], (
        "exactly one durable user row: the crash wrote none, the retry writes one")


def test_two_different_send_ids_are_two_messages(gateway):
    """(f) Dedup must never swallow the next thing the user says."""
    assert gateway.submit(text="first", client_send_id="send-1").get("error") is None
    assert gateway.submit(text="second", client_send_id="send-2").get("error") is None

    assert gateway.user_rows() == ["first", "second"]
    assert gateway.turns == ["first", "second"]


def test_a_send_without_an_id_keeps_its_exact_behaviour(gateway):
    """Older clients and every internal submit path are untouched by any of this."""
    assert gateway.submit(text="same words").get("error") is None
    assert gateway.submit(text="same words").get("error") is None

    assert gateway.user_rows() == ["same words", "same words"]
    assert gateway.turns == ["same words", "same words"]


def test_a_burst_of_new_sends_cannot_resurrect_an_older_one(gateway):
    """Retention is bounded, but not at the expense of the window it exists for: a chat
    that keeps talking must not evict a send id a pending "Move here" could still retry.
    The in-memory memory holds ``_ACCEPTED_SEND_ID_LIMIT`` ids, so this one is long gone
    from it — only the durable ledger can still recognise the retry."""
    first = {"text": "the message being moved", "client_send_id": "send-oldest"}
    assert gateway.submit(**first).get("error") is None
    for index in range(server._ACCEPTED_SEND_ID_LIMIT + 16):
        assert gateway.submit(text=f"m{index}", client_send_id=f"send-{index}").get("error") is None

    turns_before, rows_before = list(gateway.turns), gateway.user_rows()
    replayed = gateway.submit(**first)

    assert replayed.get("error") is None, replayed
    assert gateway.turns == turns_before, "the oldest id must still be recognised as sent"
    assert gateway.user_rows() == rows_before
