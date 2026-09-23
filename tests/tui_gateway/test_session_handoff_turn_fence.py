"""The mutation boundaries an explicit handoff has to be safe against.

A fence that only guards ``prompt.submit``'s entry is not enough. Two things
have to hold at EVERY point a turn can begin — the user's submit, a queued
drain, a crash auto-continue, a notification or synthesized turn:

1. **Publish before you write.** A turn marks itself in the shared registry
   BEFORE it touches queue, history or attachments, so a claimant on another
   device proving the owner idle can never be proving it about a turn that is
   already writing.
2. **Re-prove before you write.** A refused turn must leave the session exactly
   as it found it: no truncation applied, no attachments consumed, no
   duplicate submit, no ``running`` left latched. The refusal is the whole
   point — a lease object is not authority, the registry is.

Together those make the handoff race have exactly one outcome per ordering:
the turn reserves first (and the claim is refused busy) or the claim commits
first (and the turn is refused fenced). Never both.
"""

from __future__ import annotations

import threading
import types

import pytest

from hermes_cli.active_sessions import (
    SESSION_HANDOFF_BUSY,
    SESSION_NOT_OWNED,
    handoff_active_session,
    observe_active_session_owner,
    try_acquire_active_session,
)
from tui_gateway import server


@pytest.fixture(autouse=True)
def _quiet_gateway(monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)


@pytest.fixture
def emitted(monkeypatch):
    events: list[tuple[str, str, dict | None]] = []
    monkeypatch.setattr(server, "_emit", lambda kind, sid, payload=None: events.append((kind, sid, payload)))
    return events


def _owned_session(sid: str, key: str, **extra) -> tuple[dict, object]:
    """A live session that has already claimed its slot — the state a chat is in mid-conversation."""
    lease, refusal = try_acquire_active_session(
        session_id=key, surface="desktop", config={},
        metadata={"live_session_id": sid}, track_liveness=True)
    assert lease is not None, refusal
    return _session_record(sid, key, lease, **extra), lease


def _session_record(sid: str, key: str, lease, **extra) -> dict:
    """A live session record holding ``lease``. Separate from ``_owned_session`` because both
    "devices" here share one pid: re-acquiring for the second would match ``_is_same_writer``
    and mint a fresh lease, which is not what a cross-process claimant does."""
    session = {
        "agent": types.SimpleNamespace(session_id=key, clear_interrupt=lambda: None),
        "session_key": key,
        "_sid": sid,
        "history": [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}],
        "history_lock": threading.RLock(),
        "history_version": 0,
        "running": False,
        "attached_images": ["/tmp/shot.png"],
        "cols": 80,
        "source": "desktop",
        "slash_worker": None,
        "inflight_turn": None,
        "active_session_lease": lease,
        **extra,
    }
    with server._sessions_lock:
        server._sessions[sid] = session
    return session


@pytest.fixture(autouse=True)
def _clear_sessions():
    yield
    with server._sessions_lock:
        server._sessions.clear()


def _hand_off_to_another_device(key: str) -> object:
    """What the other Desktop's ``session.handoff.claim`` does to the shared registry."""
    observed = observe_active_session_owner(key)
    claimed, refusal = handoff_active_session(
        session_id=key, surface="desktop", metadata={"live_session_id": "imac-runtime"},
        expected_lease_id=observed.lease_id, expected_generation=observed.generation,
        track_liveness=True)
    assert claimed is not None, refusal
    return claimed


# ── publish before you write ───────────────────────────────────────────────────


def test_an_admitted_turn_is_visible_as_busy_to_another_process():
    """The quiescence signal a cross-process claimant reads.

    Without this the claimant's "is the owner idle?" probe is unanswerable and
    the only safe answer would be "never hand off".
    """
    session, _lease = _owned_session("macbook-runtime", "S")
    assert observe_active_session_owner("S").busy is False

    admitted = server._admit_prompt_turn(
        "macbook-runtime", session, "hello", None, None, None, None)

    assert admitted is not None, "an owner's own turn must be admitted"
    assert observe_active_session_owner("S").busy is True, (
        "the turn must be published before the turn body can write"
    )


def test_the_busy_marker_is_cleared_when_the_turn_finishes():
    """A marker that outlived its turn would make the chat unmovable forever."""
    session, lease = _owned_session("macbook-runtime", "S")
    server._admit_prompt_turn("macbook-runtime", session, "hello", None, None, None, None)
    assert observe_active_session_owner("S").busy is True

    server._finish_session_turn("macbook-runtime", session)

    assert observe_active_session_owner("S").busy is False
    assert lease is not None


def test_a_submit_publishes_its_turn_before_applying_a_truncation():
    """``_lock_in_submit_turn`` is where a rewind destroys rows. It must reserve first."""
    session, _lease = _owned_session("macbook-runtime", "S")

    err, _fields = server._lock_in_submit_turn(
        1, "macbook-runtime", session, "hello", {}, False, None, None, None)

    assert err is None, err
    assert session["running"] is True
    assert observe_active_session_owner("S").busy is True


# ── re-prove before you write ──────────────────────────────────────────────────


def test_a_fenced_submit_truncates_nothing_and_keeps_the_attachments():
    """The claim landed between this device's last turn and this submit.

    The submit carries a rewind (a destructive ``replace_messages``) and an
    attached image. Being fenced has to cost the user nothing: same history,
    same attachment, no latched ``running``.
    """
    session, _lease = _owned_session("macbook-runtime", "S")
    _hand_off_to_another_device("S")
    history_before = list(session["history"])

    err, fields = server._lock_in_submit_turn(
        1, "macbook-runtime", session, "hello",
        {"truncate_before_user_ordinal": 1, "confirm_truncate": True}, True, None, None, None)

    assert err is not None, "a fenced submit must be refused"
    assert err["error"]["code"] == 4090
    assert err["error"]["data"]["reason"] == SESSION_NOT_OWNED
    assert fields == {}
    assert session["history"] == history_before, "no row may be cut for a turn that never ran"
    assert session["attached_images"] == ["/tmp/shot.png"], "the attachment stays for the retry"
    assert session["running"] is False, "and the session must not be left latched busy"


def test_a_fenced_turn_admission_consumes_nothing_and_starts_no_turn(emitted):
    """The gate every turn source crosses (queued, auto-continue, notification, synthesized).

    A refusal here must not consume the attachments the next attempt needs, and
    must report itself once rather than silently dropping the prompt.
    """
    session, _lease = _owned_session("macbook-runtime", "S")
    _hand_off_to_another_device("S")

    admitted = server._admit_prompt_turn(
        "macbook-runtime", session, "hello", None, None, None, None)

    assert admitted is None, "a fenced turn must not be admitted"
    assert session["attached_images"] == ["/tmp/shot.png"], "attachments are not consumed by a refusal"
    assert session["running"] is False
    assert [kind for kind, *_ in emitted] == ["error"], "refused once, reported once"
    assert observe_active_session_owner("S").busy is False, (
        "and the CLAIMANT's slot must not be marked busy by the fenced runtime's attempt"
    )


def test_exactly_one_turn_begins_when_a_claim_races_a_submit(emitted):
    """The race the whole design exists for, in both orderings.

    Ordering A: this device's submit reserves first, so the claim is refused
    busy and the turn runs. Ordering B: the claim commits first, so the turn is
    refused and the claimant's turn is the only one that begins.
    """
    session, _lease = _owned_session("macbook-runtime", "S")

    # Ordering A — the turn got there first.
    assert server._admit_prompt_turn("macbook-runtime", session, "a", None, None, None, None)
    observed = observe_active_session_owner("S")
    claimed, refusal = handoff_active_session(
        session_id="S", surface="desktop", metadata={"live_session_id": "imac-runtime"},
        expected_lease_id=observed.lease_id, expected_generation=observed.generation,
        track_liveness=True)
    assert claimed is None and refusal.reason == "SESSION_HANDOFF_BUSY"

    # Ordering B — the turn finishes, the claim lands, the next turn is fenced.
    server._finish_session_turn("macbook-runtime", session)
    new_lease = _hand_off_to_another_device("S")
    assert server._admit_prompt_turn("macbook-runtime", session, "b", None, None, None, None) is None

    claimant_session = _session_record("imac-runtime", "S", new_lease)
    assert server._admit_prompt_turn("imac-runtime", claimant_session, "b", None, None, None, None), (
        "the device the user moved the chat to must be able to run its turn"
    )
    assert observe_active_session_owner("S").busy is True


# ── the queued drain ───────────────────────────────────────────────────────────


def _queue(session: dict, *texts: str) -> None:
    """Park messages the user sent while the previous turn was running."""
    server._ac_set_queue(session, [{"text": text, "transport": None} for text in texts])


def _queued_texts(session: dict) -> list[str]:
    head = session.get("queued_prompt")
    return [entry["text"] for entry in ([head] if head else []) + list(session.get("queued_prompts") or [])]


def test_a_drain_refused_by_the_fence_keeps_the_queued_message(emitted):
    """The message-loss ordering: dequeue, THEN discover the chat moved.

    A queued prompt is the user's words waiting for the live turn to finish. The
    drain used to take it out of the queue and latch ``running`` before it had
    re-proven ownership, so a handoff landing in that window left the words
    nowhere: dequeued, never dispatched, only an error event to show for it.
    """
    session, _lease = _owned_session("macbook-runtime", "S")
    _queue(session, "and one more thing", "then this")
    _hand_off_to_another_device("S")

    server._drain_queued_prompt(None, "macbook-runtime", session)

    assert _queued_texts(session) == ["and one more thing", "then this"], (
        "a refused drain must leave the queue exactly as it found it"
    )
    assert session["running"] is False, "and must not latch the session busy"
    assert observe_active_session_owner("S").busy is False, (
        "nor mark the new owner's slot busy on the way out"
    )


def test_the_drain_reserves_ownership_before_it_touches_the_queue(monkeypatch, emitted):
    """Barrier test: the reservation is the gate, and it comes FIRST.

    The claim is committed from inside the reservation call itself, which is the
    tightest possible race — the other device wins by microseconds. Whatever the
    outcome, the queue may not be disturbed before that gate has been passed, so
    the message survives a lost race.
    """
    session, _lease = _owned_session("macbook-runtime", "S")
    _queue(session, "and one more thing")
    real_reserve = server._reserve_session_turn
    observed_queue_at_reservation: list[list[str]] = []

    def claim_then_reserve(sid, target_session):
        observed_queue_at_reservation.append(_queued_texts(target_session))
        _hand_off_to_another_device("S")

        return real_reserve(sid, target_session)

    monkeypatch.setattr(server, "_reserve_session_turn", claim_then_reserve)

    server._drain_queued_prompt(None, "macbook-runtime", session)

    assert observed_queue_at_reservation, "the drain must reserve the turn at all"
    assert observed_queue_at_reservation[0] == ["and one more thing"], (
        "the queue must still be intact when ownership is re-proven"
    )
    assert _queued_texts(session) == ["and one more thing"]
    assert session["running"] is False


def test_an_owned_drain_still_dispatches_its_message(monkeypatch):
    """The fix must not break the ordinary drain: an owner's queue still fires."""
    session, _lease = _owned_session("macbook-runtime", "S")
    _queue(session, "and one more thing")
    dispatched: list[str] = []
    monkeypatch.setattr(server, "_run_prompt_submit", lambda rid, sid, sess, text, **kw: dispatched.append(text) or True)

    assert server._drain_queued_prompt(None, "macbook-runtime", session) is True

    assert dispatched == ["and one more thing"]
    assert _queued_texts(session) == [], "a dispatched message leaves the queue"


def test_a_failed_queued_compute_dispatch_restores_before_releasing_ownership(monkeypatch, emitted):
    """A broken compute pipe must not lose the dequeued envelope or open a claim window."""

    class _BrokenSupervisor:
        def submit_turn(self, _frame, *, on_complete=None):
            raise BrokenPipeError("broken pipe")

    session, _lease = _owned_session("macbook-runtime", "S")
    _queue(session, "first queued", "second queued")
    release_snapshots: list[tuple[bool, list[str]]] = []
    real_finish = server._finish_session_turn

    def observe_release(sid, target_session):
        release_snapshots.append((bool(target_session.get("running")), _queued_texts(target_session)))
        return real_finish(sid, target_session)

    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _session: True)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda _cfg=None: _BrokenSupervisor())
    monkeypatch.setattr(server, "_finish_session_turn", observe_release)

    assert server._drain_queued_prompt(None, "macbook-runtime", session) is True

    assert release_snapshots == [(False, ["first queued", "second queued"])], (
        "the exact envelope must be restored and running cleared before the busy fence drops"
    )
    assert _queued_texts(session) == ["first queued", "second queued"]
    assert session["running"] is False
    assert observe_active_session_owner("S").busy is False


def test_a_false_inline_dispatch_restores_before_releasing_ownership(monkeypatch):
    """Every non-start result keeps the dequeued head, not only ownership refusals."""
    session, _lease = _owned_session("macbook-runtime", "S")
    _queue(session, "first queued", "second queued")
    release_snapshots: list[tuple[bool, list[str]]] = []
    real_finish = server._finish_session_turn

    def observe_release(sid, target_session):
        release_snapshots.append((bool(target_session.get("running")), _queued_texts(target_session)))
        return real_finish(sid, target_session)

    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _session: False)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(server, "_finish_session_turn", observe_release)

    assert server._drain_queued_prompt(None, "macbook-runtime", session) is True
    assert release_snapshots == [(False, ["first queued", "second queued"])]
    assert _queued_texts(session) == ["first queued", "second queued"]
    assert observe_active_session_owner("S").busy is False


# ── the end of a turn ──────────────────────────────────────────────────────────


def test_old_turn_release_cannot_clear_a_new_same_runtime_reservation():
    session, _lease = _owned_session("macbook-runtime", "S")

    assert server._reserve_session_turn("macbook-runtime", session) is None
    token_a = session["_active_turn_reservation_token"]
    session["running"] = False

    refused = server._reserve_session_turn("macbook-runtime", session)
    assert getattr(refused, "reason", None) == "SESSION_HANDOFF_BUSY"

    server._finish_session_turn("macbook-runtime", session, token_a)
    assert server._reserve_session_turn("macbook-runtime", session) is None
    token_b = session["_active_turn_reservation_token"]
    assert token_b != token_a

    server._finish_session_turn("macbook-runtime", session, token_a)
    assert observe_active_session_owner("S").busy is True

    server._finish_session_turn("macbook-runtime", session, token_b)
    assert observe_active_session_owner("S").busy is False


def test_the_reservation_is_held_through_every_final_mutation(monkeypatch, emitted):
    """Adversarial handoff during finalization.

    ``running`` going False is not the end of a turn. After it the turn still
    retires its durable crash marker and publishes its settled session info —
    writes that belong to the OLD turn. Releasing the reservation before those
    finish advertises an idle owner that is still writing, and a claim landing
    there would interleave two devices' bookkeeping on one chat.

    Each observer below is a real finalization step; at every one the chat must
    still read busy to another process.
    """
    session, _lease = _owned_session("macbook-runtime", "S")
    busy_at: dict[str, bool] = {}

    def observe(step):
        def _step(*_args, **_kwargs):
            busy_at[step] = observe_active_session_owner("S").busy

        return _step

    monkeypatch.setattr(server, "_prepare_turn_input", lambda *a, **k: None)
    monkeypatch.setattr(server, "_release_turn_scopes", observe("finish_turn"))
    monkeypatch.setattr(server, "_record_turn_marker", lambda *a, **k: "marker")
    monkeypatch.setattr(server, "_retire_turn_marker", observe("retire_marker"))
    monkeypatch.setattr(server, "_emit_settled_session_info", observe("settled_info"))
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a, **k: True)

    assert server._run_prompt_submit(None, "macbook-runtime", session, "hello") is True
    session["_run_thread"].join(timeout=10)
    assert not session["_run_thread"].is_alive()

    assert busy_at == {"finish_turn": True, "retire_marker": True, "settled_info": True}, (
        f"the chat must stay unmovable until the turn's last write lands: {busy_at}"
    )
    # And once the turn really is done, the chat is movable again.
    assert observe_active_session_owner("S").busy is False


def test_a_claim_during_finalization_is_refused_busy(monkeypatch, emitted):
    """The same window, from the claimant's side: it must lose, not win."""
    session, _lease = _owned_session("macbook-runtime", "S")
    claim_outcome: dict[str, str] = {}

    def claim_mid_finalization(*_args, **_kwargs):
        observed = observe_active_session_owner("S")
        claimed, refusal = handoff_active_session(
            session_id="S", surface="desktop", metadata={"live_session_id": "imac-runtime"},
            expected_lease_id=observed.lease_id, expected_generation=observed.generation,
            track_liveness=True)
        claim_outcome["reason"] = "claimed" if claimed is not None else refusal.reason

    monkeypatch.setattr(server, "_prepare_turn_input", lambda *a, **k: None)
    monkeypatch.setattr(server, "_record_turn_marker", lambda *a, **k: "marker")
    monkeypatch.setattr(server, "_emit_settled_session_info", claim_mid_finalization)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a, **k: True)

    server._run_prompt_submit(None, "macbook-runtime", session, "hello")
    session["_run_thread"].join(timeout=10)

    assert claim_outcome == {"reason": "SESSION_HANDOFF_BUSY"}
    assert server._ensure_active_session_slot("macbook-runtime", session) is None, (
        "and the owner keeps the chat, so its next turn still runs here"
    )


# ── reservation ownership: every path without a worker clears the marker ───────


@pytest.mark.parametrize(
    ("label", "params", "has_truncation", "session_extra"),
    [
        ("malformed confirm_truncate", {"confirm_truncate": True}, False, {}),
        ("a watch child still running", {}, False, {"lazy": True}),
        ("a truncation that cannot be applied", {"truncate_before_row_id": 999999, "confirm_truncate": True}, True, {}),
    ],
)
def test_a_submit_refused_after_reserving_leaves_the_chat_movable(
    monkeypatch, label, params, has_truncation, session_extra):
    """A reservation is owned until a WORKER takes it over.

    ``_lock_in_submit_turn`` publishes the turn before it touches history, which
    is right — but each of these refusals returns without a worker ever
    starting. A marker left behind would make the chat permanently unmovable
    from the other device AND permanently un-handoffable from this one: the
    registry says "mid-reply" about a turn that never began.
    """
    session, _lease = _owned_session("macbook-runtime", "S", **session_extra)
    monkeypatch.setattr(server, "_child_run_active", lambda *_args: True)

    err, fields = server._lock_in_submit_turn(
        1, "macbook-runtime", session, "hello", params, has_truncation, None, None, None)

    assert err is not None, f"{label} must be refused"
    assert fields == {}
    assert observe_active_session_owner("S").busy is False, (
        f"{label} left the turn marker behind — the chat can never be moved again"
    )
    assert session["running"] is False


def test_an_accepted_submit_hands_the_reservation_to_its_worker(monkeypatch):
    """The other half: once the turn is locked in, the marker stays for the worker."""
    session, _lease = _owned_session("macbook-runtime", "S")

    err, _fields = server._lock_in_submit_turn(
        1, "macbook-runtime", session, "hello", {}, False, None, None, None)

    assert err is None, err
    assert session["running"] is True
    assert observe_active_session_owner("S").busy is True


def test_a_turn_that_never_reaches_its_agent_leaves_the_chat_movable(monkeypatch, emitted):
    """The deferred-build paths: a build that fails, and one cancelled before it lands.

    Both return from the turn thread without ever calling ``_run_prompt_submit``,
    so nothing downstream will ever clear the marker this submit published.
    """
    for failure in ("build_failed", "cancelled_before_ready"):
        session, _lease = _owned_session(f"macbook-{failure}", f"S-{failure}")
        err, _ = server._lock_in_submit_turn(
            1, f"macbook-{failure}", session, "hello", {}, False, None, None, None)
        assert err is None
        assert observe_active_session_owner(f"S-{failure}").busy is True

        if failure == "build_failed":
            monkeypatch.setattr(
                server, "_wait_agent_for_prompt",
                lambda *a, **k: {"error": {"message": "agent initialization failed"}})
        else:
            monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *a, **k: None)
            session["_turn_cancel_requested"] = True
        monkeypatch.setattr(server, "_emit_terminal_turn_error", lambda *a, **k: None)
        monkeypatch.setattr(server, "_session_info", lambda *a, **k: {})

        server._run_after_agent_ready(1, f"macbook-{failure}", session, "hello", None, None, None)

        assert observe_active_session_owner(f"S-{failure}").busy is False, (
            f"{failure} left the turn marker behind"
        )
        assert session["running"] is False


class _UnstartableThread:
    """Constructs fine, refuses to run — the shape of an out-of-threads process."""

    def __init__(self, *_args, **_kwargs):
        pass

    def start(self):
        raise RuntimeError("can't start new thread")

    def is_alive(self):
        return False


def _inject_worker_start_failure(monkeypatch, failure: str) -> None:
    """One of the three ways ``_submit_prompt`` can fail to produce a worker."""
    def boom(*_a, **_k):
        raise RuntimeError("no worker for you")

    if failure == "build_start":
        monkeypatch.setattr(server, "_start_agent_build", boom)
    elif failure == "thread_construction":
        monkeypatch.setattr(threading, "Thread", boom)
    elif failure == "thread_start":
        monkeypatch.setattr(threading, "Thread", _UnstartableThread)
    else:  # pragma: no cover - guards the parametrization itself
        raise AssertionError(f"unknown failure {failure}")


@pytest.fixture
def submittable(monkeypatch):
    """Quiet the parts of ``prompt.submit`` that are not the reservation handoff."""
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a, **k: True)
    monkeypatch.setattr(server, "_persist_session_row_for_submit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_run_after_agent_ready", lambda *a, **k: None)


@pytest.mark.parametrize("failure", ["build_start", "thread_construction", "thread_start"])
def test_a_submit_whose_worker_never_starts_leaves_the_chat_movable(
    monkeypatch, submittable, failure):
    """The gap between locking the turn in and having something that can run it.

    ``_lock_in_submit_turn`` publishes the turn, latches ``running`` and hands
    the reservation to a worker that does not exist yet — the worker is born
    three statements later, in a build start, a ``threading.Thread``
    construction and a ``start()``. Any of those can raise, and then nobody is
    left to finalize: the registry keeps saying "mid-reply" about a turn that
    never began, so the chat is unmovable from the other device forever.
    """
    session, _lease = _owned_session("macbook-runtime", "S")
    _queue(session, "and one more thing")
    _inject_worker_start_failure(monkeypatch, failure)

    response = server._submit_prompt(1, {"session_id": "macbook-runtime", "text": "hello"})

    assert response.get("error") is not None, (
        "a submit with no worker must not answer 'streaming' — the client would wait forever"
    )
    assert response.get("result") is None
    assert session["running"] is False, "left the session latched busy"
    assert session["inflight_turn"] is None, "left a phantom turn in flight"
    assert session.get("_submit_user_row") is None, (
        "a later turn would adopt this submit's row as its own input"
    )
    assert observe_active_session_owner("S").busy is False, (
        "left the turn marker behind — the chat can never be moved again"
    )
    assert session.get("_run_thread") is None, (
        "kept a thread handle that will never run, so session.interrupt cannot tell "
        "a live turn from a stuck flag"
    )
    assert _queued_texts(session) == ["and one more thing"], "the queued message must survive"
    assert session["attached_images"] == ["/tmp/shot.png"], "the attachment stays for the retry"


@pytest.mark.parametrize("failure", ["build_start", "thread_construction", "thread_start"])
def test_a_submit_whose_worker_never_starts_finishes_its_turn_once(
    monkeypatch, submittable, failure):
    """Fail closed, not fail twice: a second finish would end the NEXT turn's marker."""
    session, _lease = _owned_session("macbook-runtime", "S")
    real_finish = server._finish_session_turn
    finishes: list[str] = []
    monkeypatch.setattr(
        server, "_finish_session_turn",
        lambda sid, sess: (finishes.append(sid), real_finish(sid, sess))[1])
    _inject_worker_start_failure(monkeypatch, failure)

    server._submit_prompt(1, {"session_id": "macbook-runtime", "text": "hello"})

    assert finishes == ["macbook-runtime"], f"finished {len(finishes)} times"


def test_a_submit_whose_worker_starts_keeps_its_reservation(monkeypatch, submittable):
    """The other half: the guard may only fire when the worker really failed to start.

    Clearing the marker after a successful start would advertise an idle owner
    while the turn is running — the double-writer window the fence exists for.
    """
    session, _lease = _owned_session("macbook-runtime", "S")
    finishes: list[str] = []
    monkeypatch.setattr(server, "_finish_session_turn", lambda sid, sess: finishes.append(sid))
    started: list[bool] = []
    monkeypatch.setattr(
        threading, "Thread",
        lambda *a, **k: types.SimpleNamespace(
            start=lambda: started.append(True), is_alive=lambda: True))

    response = server._submit_prompt(1, {"session_id": "macbook-runtime", "text": "hello"})

    assert response.get("error") is None, response
    assert response["result"]["status"] == "streaming"
    assert started == [True]
    assert session["running"] is True
    assert session.get("_run_thread") is not None, "the live turn's handle must stay"
    assert finishes == [], "the worker owns the reservation now; the submit must not finish it"
    assert observe_active_session_owner("S").busy is True


def test_a_failed_isolated_dispatch_leaves_the_chat_movable(monkeypatch):
    """The compute-host twin of the leak above.

    An isolated turn is published by the PARENT (the child only ever holds an inert
    borrowed token), so a dispatch that never reaches a child leaves nobody to
    report the turn done — and nobody to clear the marker it published.
    """
    session, _lease = _owned_session("macbook-runtime", "S")
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_compute_host_turn_frame", lambda *a, **k: {})

    class _Broken:
        def submit_turn(self, *_a, **_k):
            raise RuntimeError("no compute host")

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *_a, **_k: _Broken())

    response = server._submit_prompt_to_compute_host(1, "macbook-runtime", session, "hello")

    assert response["error"]["code"] == 5019
    assert observe_active_session_owner("S").busy is False, (
        "a dispatch that never started a turn must not leave the chat mid-reply"
    )


def _claim_from_another_device(key: str) -> str | None:
    """What another device's ``session.handoff.claim`` gets right now: the refusal reason,
    or None when the claim WON and the chat moved out from under this runtime."""
    observed = observe_active_session_owner(key)
    claimed, refusal = handoff_active_session(
        session_id=key, surface="desktop", metadata={"live_session_id": "imac-runtime"},
        expected_lease_id=observed.lease_id, expected_generation=observed.generation,
        track_liveness=True)
    return None if claimed is not None else str(getattr(refusal, "reason", "") or "")


@pytest.fixture
def broken_isolated_dispatch(monkeypatch):
    """Turn isolation on, and a compute host whose dispatch never reaches a child."""
    monkeypatch.setattr(
        server, "_load_dashboard_process_isolation_config", lambda: {"turn_isolation": True})
    monkeypatch.setattr(server, "_compute_host_turn_frame", lambda *a, **k: {})

    class _Broken:
        def submit_turn(self, *_a, **_k):
            raise RuntimeError("no compute host")

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *_a, **_k: _Broken())


def test_a_submit_falling_back_inline_keeps_the_marker_it_locked_in(
    monkeypatch, emitted, broken_isolated_dispatch):
    """The handoff window between a failed isolated dispatch and its inline fallback.

    ``_lock_in_submit_turn`` reserved this turn and handed the marker to the worker
    it is about to start. The isolated dispatch is only an attempt at being that
    worker: when it fails, ``prompt.submit`` falls back to the inline runner, which
    then persists the durable user row and starts a thread. The dispatch clearing a
    reservation it did not own left both of those writes running on a chat the
    registry advertises as idle — so another device's claim wins mid-write and two
    runtimes own the same conversation.
    """
    session, _lease = _owned_session("macbook-runtime", "S", _compute_host_active=True)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a, **k: True)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_run_after_agent_ready", lambda *a, **k: None)
    real_finish = server._finish_session_turn
    finishes: list[str] = []
    monkeypatch.setattr(
        server, "_finish_session_turn",
        lambda sid, sess: (finishes.append(sid), real_finish(sid, sess))[1])
    # Observers at the two points the fallback writes on the shared chat.
    at_user_row: list[tuple[bool, str | None]] = []
    at_worker_start: list[tuple[bool, str | None]] = []

    def observe_user_row(*_a, **_k):
        at_user_row.append((observe_active_session_owner("S").busy, _claim_from_another_device("S")))

    def observe_worker_start():
        at_worker_start.append(
            (observe_active_session_owner("S").busy, _claim_from_another_device("S")))

    monkeypatch.setattr(server, "_persist_submit_user_row", observe_user_row)
    monkeypatch.setattr(
        threading, "Thread",
        lambda *a, **k: types.SimpleNamespace(start=observe_worker_start, is_alive=lambda: True))

    response = server._submit_prompt(1, {"session_id": "macbook-runtime", "text": "hello"})

    assert response.get("error") is None, response
    assert response["result"]["status"] == "streaming"
    assert at_user_row == [(True, SESSION_HANDOFF_BUSY)], (
        "the durable user row was written on a chat advertised as idle, so a claim from "
        f"another device committed mid-write: {at_user_row}"
    )
    assert at_worker_start == [(True, SESSION_HANDOFF_BUSY)], (
        f"the fallback worker started without the busy marker it inherited: {at_worker_start}"
    )
    assert finishes == [], (
        "a failed dispatch must hand the caller's reservation back untouched; the worker "
        f"owns the release now: finished {finishes}"
    )
    assert observe_active_session_owner("S").busy is True, (
        "the inline fallback owns the turn; the chat stays unmovable until it completes"
    )
    assert session["running"] is True


# ── the post-turn follow-up phase ─────────────────────────────────────────────
#
# ``running`` dropping and the busy marker clearing are the END of a turn's own
# writes — but not the end of the turn's WORK. After them the runtime still
# requeues a leftover /steer, drains the user's queued message, latches
# ``running`` for a goal continuation and consumes completion notifications.
# Every one of those mutates state the registry says belongs to whoever owns
# the chat NOW, so each must happen under a re-proven reservation, and a
# follow-up refused by the fence must be left where the new owner can still
# find it rather than dropped.


@pytest.fixture
def drained(monkeypatch):
    """Record each post-turn notification drain and the busy marker it ran under."""
    from tools.process_registry import process_registry
    drains: list[bool] = []

    real_restore = process_registry.restore_completions

    def record(*args, **kwargs):
        # ``drained_for`` (the post-turn safety net) opens with this call.
        drains.append(observe_active_session_owner("S").busy)
        return real_restore(*args, **kwargs)

    monkeypatch.setattr(process_registry, "restore_completions", record)
    return drains


def _followups(session, result=None, goal=None, sid="macbook-runtime") -> None:
    server._run_post_turn_followups(None, sid, session, result if result is not None else {}, goal)


def test_a_handoff_before_the_follow_up_phase_strands_no_work_on_the_old_runtime(
    monkeypatch, emitted, drained):
    """The window this blocker is about: finalization released the marker, the
    claim lands, and only THEN does the old runtime run its follow-ups.

    A leftover /steer, the user's queued message and the completion queue are
    all shared state the new owner now owns. The fenced runtime must touch none
    of them — and must not drop the steer on the floor without saying so.
    """
    session, _lease = _owned_session("macbook-runtime", "S")
    _queue(session, "and one more thing")
    dispatched: list[str] = []
    monkeypatch.setattr(
        server, "_run_prompt_submit", lambda rid, sid, sess, text, **kw: dispatched.append(text) or True)
    _hand_off_to_another_device("S")

    _followups(session, {"pending_steer": "actually, do X"})

    assert _queued_texts(session) == ["and one more thing"], (
        "a fenced follow-up phase must neither enqueue the steer nor drain the queue"
    )
    assert dispatched == [], "and must start no turn on a runtime that no longer owns the chat"
    assert session["running"] is False
    assert drained == [], "the new owner's completion events are not this runtime's to consume"
    assert observe_active_session_owner("S").busy is False, (
        "nor may the fenced runtime mark the CLAIMANT's slot busy on its way out"
    )
    assert [kind for kind, *_ in emitted] == ["error"], (
        "work the fence stranded must be reported, not silently discarded"
    )


def test_the_follow_up_phase_reserves_ownership_before_it_enqueues_a_steer(
    monkeypatch, emitted, drained):
    """Barrier test: the claim commits from inside the reservation call itself.

    Whichever side wins by microseconds, the steer may not have been appended to
    the queue before that gate was passed — otherwise a lost race leaves the new
    owner's queue carrying a message the old runtime wrote into it.
    """
    session, _lease = _owned_session("macbook-runtime", "S")
    real_reserve = server._reserve_session_turn
    queue_at_reservation: list[list[str]] = []

    def claim_then_reserve(sid, target_session):
        queue_at_reservation.append(_queued_texts(target_session))
        _hand_off_to_another_device("S")
        return real_reserve(sid, target_session)

    monkeypatch.setattr(server, "_reserve_session_turn", claim_then_reserve)

    _followups(session, {"pending_steer": "actually, do X"})

    assert queue_at_reservation, "the follow-up phase must reserve the turn at all"
    assert queue_at_reservation[0] == [], "the queue must be untouched when ownership is re-proven"
    assert _queued_texts(session) == [], "and a lost race must leave it that way"
    assert session["running"] is False


def test_a_handoff_before_the_follow_up_phase_never_latches_running_for_a_goal(
    monkeypatch, emitted, drained):
    """The goal continuation latches ``running`` BEFORE its nested admission
    re-proves ownership. On a fenced runtime that flips shared session state
    for a turn that will then be refused."""
    session, _lease = _owned_session("macbook-runtime", "S")
    running_at_dispatch: list[bool] = []
    monkeypatch.setattr(
        server, "_run_prompt_submit",
        lambda rid, sid, sess, text, **kw: running_at_dispatch.append(sess["running"]) or True)
    _hand_off_to_another_device("S")

    _followups(session, {}, goal="keep going")

    assert running_at_dispatch == [], "a fenced runtime must not dispatch a goal continuation"
    assert session["running"] is False, "nor leave the session latched busy for one"
    assert observe_active_session_owner("S").busy is False


def test_the_follow_up_phase_holds_the_busy_marker_while_it_writes(monkeypatch, emitted, drained):
    """The owner's own path: every follow-up mutation is published to the
    registry first, so a claimant probing mid-phase proves the owner busy."""
    session, _lease = _owned_session("macbook-runtime", "S")
    busy_at_enqueue: list[bool] = []
    real_enqueue = server._enqueue_prompt

    def observe_enqueue(sess, text, transport, **kwargs):
        busy_at_enqueue.append(observe_active_session_owner("S").busy)
        return real_enqueue(sess, text, transport, **kwargs)

    monkeypatch.setattr(server, "_enqueue_prompt", observe_enqueue)
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a, **k: False)

    _followups(session, {"pending_steer": "actually, do X"})

    assert busy_at_enqueue == [True], "the steer requeue ran on an owner advertised as idle"
    assert drained == [True], "so did the completion drain"
    assert observe_active_session_owner("S").busy is False, (
        "and the chat is movable again once the phase ends"
    )


def test_a_dispatched_goal_continuation_keeps_the_marker_for_its_turn(monkeypatch, emitted, drained):
    """Hand over, never double-finish: the continuation turn owns the release."""
    session, _lease = _owned_session("macbook-runtime", "S")
    busy_at_dispatch: list[bool] = []
    monkeypatch.setattr(
        server, "_run_prompt_submit",
        lambda rid, sid, sess, text, **kw: busy_at_dispatch.append(
            observe_active_session_owner("S").busy) or True)

    _followups(session, {}, goal="keep going")

    assert busy_at_dispatch == [True], "the continuation was dispatched on an idle-looking owner"
    assert session["running"] is True
    assert observe_active_session_owner("S").busy is True, (
        "releasing here would advertise an idle owner while its next turn writes"
    )


def test_a_drained_queued_prompt_keeps_the_marker_for_its_turn(monkeypatch, emitted, drained):
    """Same handover through the queued drain, which reserves for itself."""
    session, _lease = _owned_session("macbook-runtime", "S")
    _queue(session, "and one more thing")
    dispatched: list[str] = []
    monkeypatch.setattr(
        server, "_run_prompt_submit", lambda rid, sid, sess, text, **kw: dispatched.append(text) or True)

    _followups(session)

    assert dispatched == ["and one more thing"]
    assert observe_active_session_owner("S").busy is True, (
        "the drained turn holds the marker now; the follow-up phase must not clear it"
    )


def test_an_ordinary_follow_up_phase_finishes_its_turn_exactly_once(monkeypatch, emitted, drained):
    """No follow-up work at all: the common turn still ends movable, once."""
    session, _lease = _owned_session("macbook-runtime", "S")
    real_finish = server._finish_session_turn
    finishes: list[str] = []
    monkeypatch.setattr(
        server, "_finish_session_turn",
        lambda sid, sess: (finishes.append(sid), real_finish(sid, sess))[1])

    _followups(session)

    assert drained == [True], "an owner still drains its own completion events"
    assert finishes == ["macbook-runtime"], f"finished {len(finishes)} times"
    assert observe_active_session_owner("S").busy is False
    assert session["running"] is False
