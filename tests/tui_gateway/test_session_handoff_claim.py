"""``session.handoff.claim``: moving one chat's write lease to THIS device, on purpose.

The topology this exists for: a MacBook and an iMac both reach one remote Hermes
profile over SSH. Each Desktop ownershipId spawns its own ``hermes serve
--isolated``, so the two live in different PROCESSES sharing one
``runtime/active_sessions.json``. Opening a chat on the second device reads
history fine (resume is a reader) and every submit is refused
``SESSION_NOT_OWNED`` — correctly, because the first device is still a writer.

What was missing was a way to say "I am here now". These tests pin the
properties of that transfer, in the gateway layer:

* resume/open never claim. The refusal is the DEFAULT; only an explicit
  ``session.handoff.claim`` moves ownership.
* the claim is a compare-and-swap on what the claimant observed, so a second
  device acting on a stale refusal loses safely.
* a running owner is reported busy. Nothing is interrupted, nothing transfers.
* after the transfer the OLD runtime is fenced at its next mutation boundary —
  its in-memory lease object is not authority, the registry is.
* fan-out (several clients on ONE runtime) and same-process detached takeover
  are untouched: they were never the double-writer hazard.
"""

from __future__ import annotations

import threading
import types

import pytest

from hermes_cli.active_sessions import (
    SESSION_COORDINATION_UNAVAILABLE,
    SESSION_HANDOFF_BUSY,
    SESSION_HANDOFF_STALE,
    SESSION_NOT_OWNED,
    active_session_registry_snapshot,
    begin_active_session_turn,
    observe_active_session_owner,
    try_acquire_active_session,
)
from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport


@pytest.fixture(autouse=True)
def _quiet_gateway(monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)


def _dispatch(req: dict, transport=None) -> dict | None:
    token = bind_transport(transport)
    try:
        return server.handle_request(req)
    finally:
        reset_transport(token)


def _session(sid: str, key: str, **extra) -> dict:
    """A live desktop session record, registered like ``session.resume`` leaves one."""
    session = {
        "agent": types.SimpleNamespace(session_id=key),
        "session_key": key,
        "_sid": sid,
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


def _foreign_owner(key: str, *, surface: str = "desktop", live_id: str = "macbook-runtime"):
    """The OTHER device's lease: a distinct writer holding ``key`` in the shared registry."""
    lease, refusal = try_acquire_active_session(
        session_id=key, surface=surface, config={},
        metadata={"live_session_id": live_id}, track_liveness=True)
    assert lease is not None and refusal is None, refusal
    return lease


def _claim(sid: str, owner, **overrides) -> dict:
    params = {
        "session_id": sid,
        "expected_lease_id": owner.lease_id,
        "expected_generation": owner.generation,
    }
    params.update(overrides)
    return _dispatch({"id": 1, "method": "session.handoff.claim", "params": params})


# ── the default is refusal ─────────────────────────────────────────────────────


def test_admission_is_refused_with_the_data_a_claimant_needs():
    """The 4090 that starts the whole flow, and what the UI reads off it.

    Without the ownership block the desktop can only offer "start a new chat" —
    it has nothing to compare-and-swap against.
    """
    _foreign_owner("S", surface="desktop")
    session = _session("imac-runtime", "S")

    refusal = server._ensure_active_session_slot("imac-runtime", session)

    assert refusal is not None
    assert refusal.reason == SESSION_NOT_OWNED
    ownership = refusal.ownership
    assert ownership["session_id"] == "S"
    assert ownership["handoff_supported"] is True
    assert ownership["busy"] is False
    assert session["active_session_lease"] is None, "a refused admission must not attach a lease"


def test_resume_does_not_claim_or_steal_an_owned_session():
    """Reading is not writing.

    Opening a chat owned elsewhere must stay a read: an implicit claim on
    resume is exactly the auto-steal this feature refuses to be. The owner is
    unchanged afterwards and the viewer holds no lease.
    """
    owner = _foreign_owner("S")
    before = observe_active_session_owner("S")

    session = _session("imac-runtime", "S")
    assert session.get("active_session_lease") is None

    after = observe_active_session_owner("S")
    assert after.lease_id == before.lease_id == owner.lease_id
    assert after.generation == before.generation


# ── the explicit claim ─────────────────────────────────────────────────────────


def test_an_explicit_claim_moves_the_lease_and_fences_the_old_runtime():
    """The happy path, and the fence that makes it safe.

    After the claim the claimant may submit and the old owner's lease — which
    still exists in its own process — must fail at its next mutation boundary.
    """
    old_owner = _foreign_owner("S")
    session = _session("imac-runtime", "S")

    response = _claim("imac-runtime", observe_active_session_owner("S"))

    assert response.get("error") is None, response
    assert response["result"]["status"] == "claimed"
    assert response["result"]["generation"] == old_owner.generation + 1
    assert session["active_session_lease"] is not None

    # The claimant is admitted...
    assert server._ensure_active_session_slot("imac-runtime", session) is None
    # ...and the old runtime, still holding its lease object, is not.
    stale_session = _session("macbook-runtime", "S", active_session_lease=old_owner)
    fenced = server._ensure_active_session_slot("macbook-runtime", stale_session)
    assert fenced is not None and fenced.reason == SESSION_NOT_OWNED
    assert len(active_session_registry_snapshot()) == 1, "still exactly one writer"


def test_a_claim_on_a_stale_observation_conflicts_safely():
    """Two devices acting on the same refusal: the loser must not silently win.

    The second claim replays an observation the first claim already superseded.
    It has to be refused as stale (retryable after re-observing), never applied.
    """
    first_owner = _foreign_owner("S")
    stale_observation = observe_active_session_owner("S")
    _session("imac-runtime", "S")
    _session("third-runtime", "S")

    assert _claim("imac-runtime", stale_observation).get("error") is None

    conflicted = _claim("third-runtime", stale_observation)

    assert conflicted["error"]["code"] == 4090
    assert conflicted["error"]["data"]["reason"] == SESSION_HANDOFF_STALE
    owner_now = observe_active_session_owner("S")
    assert owner_now.generation == first_owner.generation + 1, "exactly one handoff happened"


def test_a_busy_owner_is_reported_busy_and_nothing_moves():
    """A turn in flight is never taken over.

    Transferring mid-turn would leave the old runtime streaming into a
    transcript it no longer owns. The claim refuses with a machine-readable
    busy reason and the UI keeps the user's draft.
    """
    owner = _foreign_owner("S")
    assert begin_active_session_turn(owner, turn_token="macbook-turn") is None
    session = _session("imac-runtime", "S")

    response = _claim("imac-runtime", observe_active_session_owner("S"))

    assert response["error"]["code"] == 4090
    assert response["error"]["data"]["reason"] == SESSION_HANDOFF_BUSY
    assert response["error"]["data"]["ownership"]["busy"] is True
    assert session["active_session_lease"] is None
    assert observe_active_session_owner("S").lease_id == owner.lease_id


def test_a_claim_for_a_session_with_no_live_owner_still_acquires_it():
    """The owner let go between the refusal and the click.

    Nothing to fence, so the claim degrades to an ordinary acquisition rather
    than failing the user's "Move here" on a session that is simply free.
    """
    owner = _foreign_owner("S")
    observation = observe_active_session_owner("S")
    from hermes_cli.active_sessions import release_active_session

    release_active_session(owner)
    session = _session("imac-runtime", "S")

    response = _claim("imac-runtime", observation)

    assert response.get("error") is None, response
    assert response["result"]["status"] == "claimed"
    assert session["active_session_lease"] is not None


def test_an_unprovable_registry_refuses_the_claim():
    """Fail closed: "who owns this?" unanswered is not consent to write."""
    owner = _foreign_owner("S")
    observation = observe_active_session_owner("S")
    _session("imac-runtime", "S")

    from hermes_cli.active_sessions import _state_path

    corrupt = b"{ this is not json"
    _state_path().write_bytes(corrupt)

    response = _claim("imac-runtime", observation)

    assert response["error"]["code"] == 4090
    assert response["error"]["data"]["reason"] == SESSION_COORDINATION_UNAVAILABLE
    assert _state_path().read_bytes() == corrupt, "the operator's file is left as found"
    assert owner is not None


def test_claiming_a_session_this_runtime_already_owns_is_a_no_op():
    """Idempotence for the double-click / retried RPC: no second slot, no fence bump."""
    session = _session("imac-runtime", "S")
    lease, _ = try_acquire_active_session(
        session_id="S", surface="desktop", config={},
        metadata={"live_session_id": "imac-runtime"}, track_liveness=True)
    session["active_session_lease"] = lease

    response = _claim("imac-runtime", observe_active_session_owner("S"))

    assert response.get("error") is None, response
    assert response["result"]["status"] == "already_owner"
    assert session["active_session_lease"] is lease
    assert observe_active_session_owner("S").generation == lease.generation


# ── a socket that drops around the commit ──────────────────────────────────────


def test_a_claim_that_never_reached_the_registry_leaves_the_owner_in_place():
    """Disconnect BEFORE the commit: the other device is still the owner.

    The compare-and-swap is one write under the registry's file lock, so a
    claimant that dies on the way there cannot leave a half-moved chat — and
    must not leave a mark on the file either.
    """
    owner = _foreign_owner("S")
    observed = observe_active_session_owner("S")
    _session("imac-runtime", "S")

    from hermes_cli.active_sessions import _read_entries, _state_path

    before = _read_entries(_state_path())
    # The claimant is gone before its handler can run (its socket dropped and
    # the session was reaped), so the claim never reaches the CAS.
    with server._sessions_lock:
        server._sessions.pop("imac-runtime", None)

    response = _claim("imac-runtime", observed)

    assert response["error"]["code"] == 4001
    assert _read_entries(_state_path()) == before, "an unreached claim leaves no trace"
    assert observe_active_session_owner("S").lease_id == owner.lease_id


def test_a_claimant_that_disconnects_after_the_commit_is_still_the_owner():
    """Disconnect AFTER the commit: the move is durable, not conditional on a socket.

    The reply may never reach the client, so it cannot be what makes the
    handoff real. The registry write is. The old runtime stays fenced, and the
    reconnecting claimant re-binds to the live session that already holds the
    lease — no second claim, no window in which nobody owns the chat.
    (If the claimant never comes back, the ordinary WS-orphan reaper releases
    the lease like any other abandoned session.)
    """
    old_owner = _foreign_owner("S")
    session = _session("imac-runtime", "S")

    assert _claim("imac-runtime", observe_active_session_owner("S")).get("error") is None
    claimed_lease = session["active_session_lease"]

    # The response frame is lost: the socket drops the moment after the commit.
    session["transport"] = server._detached_ws_transport

    assert observe_active_session_owner("S").lease_id == claimed_lease.lease_id
    stale_session = _session("macbook-runtime", "S", active_session_lease=old_owner)
    fenced = server._ensure_active_session_slot("macbook-runtime", stale_session)
    assert fenced is not None and fenced.reason == SESSION_NOT_OWNED
    assert server._ensure_active_session_slot("imac-runtime", session) is None, (
        "the reconnecting claimant owns the chat without claiming again"
    )


# ── what must NOT change ───────────────────────────────────────────────────────


def test_one_runtime_serving_several_clients_never_trips_the_fence():
    """Fan-out regression: many viewers of ONE runtime are one writer.

    The fence is per stored session per RUNTIME. Re-validating on every
    boundary must not turn a second desktop window attached to the SAME live
    session into a second writer — that would break every pop-out and tile.
    """
    session = _session("imac-runtime", "S")
    assert server._ensure_active_session_slot("imac-runtime", session) is None
    lease = session["active_session_lease"]

    for _ in range(3):
        assert server._ensure_active_session_slot("imac-runtime", session) is None
        assert session["active_session_lease"] is lease, "no re-claim, no new slot"

    assert len(active_session_registry_snapshot()) == 1


def test_same_process_detached_runtime_takeover_still_works():
    """The pre-existing recovery path (#104691) is untouched by fencing.

    A desktop renderer crash leaves a detached runtime holding the lease in
    THIS process. Its successor still inherits it without the user having to
    invoke a handoff — that was never the cross-device hazard.
    """
    dead_transport = server._detached_ws_transport
    old = _session("crashed-runtime", "S", transport=dead_transport)
    assert server._ensure_active_session_slot("crashed-runtime", old) is None
    inherited_lease = old["active_session_lease"]

    successor = _session("fresh-runtime", "S")
    assert server._ensure_active_session_slot("fresh-runtime", successor) is None

    assert successor["active_session_lease"] is inherited_lease
    assert "active_session_lease" not in old
    assert len(active_session_registry_snapshot()) == 1
