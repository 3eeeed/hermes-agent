"""Explicit cross-device handoff of one stored session's write lease.

``test_active_session_exclusivity`` pins the fence: at most one live owner may
write a stored session. This file pins the only sanctioned way THROUGH that
fence -- a user-invoked handoff -- and the properties that keep it from
becoming a steal:

* the claimant must name the owner it observed (lease id AND fencing
  generation). An exact compare-and-swap is the whole safety argument: a stale
  observation, a changed owner, or a replayed claim cannot move the lease.
* the generation is what FENCES the old runtime. Its in-memory lease object
  survives the handoff, so validation -- not the lease object's existence -- is
  what has to say "not yours any more".
* a busy owner is never taken over. Cross-process quiescence is PROVEN from the
  registry (the owner marks its own turn under the same file lock), never
  assumed from a pid or a timestamp.
* every refusal preserves the registry byte-for-byte. "I could not prove this
  is safe" must never mutate ownership.

Regression for the two-desktop topology: a MacBook and an iMac reach one remote
Hermes profile over SSH, each ownershipId spawning its own
``hermes serve --isolated``. The second process could read history but every
submit was refused SESSION_NOT_OWNED, with no supported way to move control.
"""

import itertools
import os

import pytest

from hermes_cli.active_sessions import (
    SESSION_COORDINATION_UNAVAILABLE,
    SESSION_HANDOFF_BUSY,
    SESSION_HANDOFF_STALE,
    SESSION_NOT_OWNED,
    _read_entries,
    _state_path,
    _write_entries,
    active_session_registry_snapshot,
    begin_active_session_turn,
    end_active_session_turn,
    handoff_active_session,
    observe_active_session_owner,
    try_acquire_active_session,
    validate_active_session_lease,
)


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))


_owner_seq = itertools.count()


def acquire(session_id, *, surface="desktop", live_id=None, config=None):
    """Acquire as a DISTINCT writer unless a live id is given (see the sibling file)."""
    return try_acquire_active_session(
        session_id=session_id,
        surface=surface,
        config=config if config is not None else {},
        metadata={"live_session_id": live_id or f"live-{next(_owner_seq)}"},
        track_liveness=True,
    )


def claim(session_id, owner, *, surface="desktop", live_id=None, generation=None, lease_id=None):
    """Hand the lease to a new writer, echoing what it observed about ``owner``."""
    return handoff_active_session(
        session_id=session_id,
        surface=surface,
        metadata={"live_session_id": live_id or f"live-{next(_owner_seq)}"},
        expected_lease_id=lease_id if lease_id is not None else owner.lease_id,
        expected_generation=generation if generation is not None else owner.generation,
        track_liveness=True,
    )


def test_an_exact_observation_moves_the_lease_exactly_once():
    """The sanctioned path, and the replay that must not work twice.

    The second claim replays a now-historical (lease_id, generation) pair. That
    is what a double-clicked "Move here", a retried RPC, or a second device
    acting on a cached refusal looks like -- and treating it as valid would
    hand the lease to a writer whose evidence is stale.
    """
    owner_lease, refused = acquire("S")
    assert owner_lease is not None and refused is None

    observed = observe_active_session_owner("S")
    assert observed is not None
    assert observed.lease_id == owner_lease.lease_id
    assert observed.generation == owner_lease.generation
    assert observed.busy is False

    claimed, refusal = claim("S", observed)
    assert claimed is not None, f"an exact observation must move the lease: {refusal}"
    assert claimed.generation == observed.generation + 1, (
        "the generation must advance, so the fenced runtime's lease can never validate again"
    )

    replayed, replay_refusal = claim("S", observed)
    assert replayed is None, "a replayed observation must not move the lease a second time"
    assert replay_refusal.reason == SESSION_HANDOFF_STALE

    # And the claimant is still the owner: a refused replay changed nothing.
    still = observe_active_session_owner("S")
    assert still.lease_id == claimed.lease_id
    assert still.generation == claimed.generation
    assert len(active_session_registry_snapshot()) == 1, "a handoff moves a slot, never adds one"


def test_a_stale_generation_is_refused_without_touching_the_owner():
    """Same lease id, wrong generation: the claimant's evidence predates a fence."""
    owner_lease, _ = acquire("S")
    observed = observe_active_session_owner("S")

    before = _read_entries(_state_path())
    stale, refusal = claim("S", observed, generation=observed.generation - 1)

    assert stale is None
    assert refusal.reason == SESSION_HANDOFF_STALE
    assert _read_entries(_state_path()) == before, "a refused claim must preserve the registry"
    assert observe_active_session_owner("S").lease_id == owner_lease.lease_id


def test_a_foreign_lease_id_is_refused_without_touching_the_owner():
    """Right generation, wrong owner: the claimant observed somebody else."""
    acquire("S")
    observed = observe_active_session_owner("S")

    before = _read_entries(_state_path())
    stale, refusal = claim("S", observed, lease_id="0" * 32)

    assert stale is None
    assert refusal.reason == SESSION_HANDOFF_STALE
    assert _read_entries(_state_path()) == before


def test_an_owner_mid_turn_is_reported_busy_and_kept():
    """Quiescence is proven, not assumed.

    The owner marks its own turn in the registry under the same file lock the
    claim takes, so "is the owner idle?" is answerable from another process
    without interrupting anything. A running turn is a structured refusal --
    never an optimistic transfer, never an interrupt.
    """
    owner_lease, _ = acquire("S")
    assert begin_active_session_turn(owner_lease, turn_token="tab-1") is None

    busy_observation = observe_active_session_owner("S")
    assert busy_observation.busy is True

    before = _read_entries(_state_path())
    denied, refusal = claim("S", busy_observation)

    assert denied is None
    assert refusal.reason == SESSION_HANDOFF_BUSY
    assert _read_entries(_state_path()) == before, "a busy refusal must preserve the registry"
    assert validate_active_session_lease(owner_lease) is None, "the owner keeps its lease"

    # When the turn finishes the same observation is still stale (generation is
    # unchanged, so this one is fine) and the handoff goes through.
    end_active_session_turn(owner_lease, turn_token="tab-1")
    assert observe_active_session_owner("S").busy is False
    claimed, refusal = claim("S", observe_active_session_owner("S"))
    assert claimed is not None, f"an idle owner must be claimable: {refusal}"


def test_the_fenced_runtime_fails_validation_while_the_claimant_passes():
    """What the old runtime's in-memory lease must do after the transfer.

    The handoff cannot reach into another process and delete its lease object,
    so the object outlives the transfer. Everything depends on validation
    against the registry saying "not yours" -- that is the fence.
    """
    old_lease, _ = acquire("S")
    assert validate_active_session_lease(old_lease) is None

    new_lease, _ = claim("S", observe_active_session_owner("S"))

    fenced = validate_active_session_lease(old_lease)
    assert fenced is not None, "the old runtime's lease must stop validating after the handoff"
    assert fenced.reason == SESSION_NOT_OWNED
    assert validate_active_session_lease(new_lease) is None, "and the claimant must own it"


def test_a_fenced_runtime_cannot_begin_a_turn():
    """The fence has to hold at the mutation boundary, not just on a status read."""
    old_lease, _ = acquire("S")
    claim("S", observe_active_session_owner("S"))

    refusal = begin_active_session_turn(old_lease, turn_token="tab-1")
    assert refusal is not None and refusal.reason == SESSION_NOT_OWNED
    assert observe_active_session_owner("S").busy is False, (
        "a refused turn must not leave the new owner's slot marked busy"
    )


def test_a_fenced_runtime_cannot_clear_the_new_owners_busy_marker():
    """End-of-turn bookkeeping is lease-scoped, or a dying runtime unlocks a live turn."""
    old_lease, _ = acquire("S")
    new_lease, _ = claim("S", observe_active_session_owner("S"))
    begin_active_session_turn(new_lease, turn_token="tab-2")

    end_active_session_turn(old_lease, turn_token="tab-1")
    end_active_session_turn(old_lease, turn_token="tab-2")

    assert observe_active_session_owner("S").busy is True


def test_an_unreadable_registry_fails_closed_and_preserves_its_bytes():
    """"I cannot tell who owns this" is not a go-ahead, and not a licence to rewrite."""
    owner_lease, _ = acquire("S")
    observed = observe_active_session_owner("S")

    corrupt = b'{"entries": [{"lease_id": '
    _state_path().write_bytes(corrupt)

    claimed, refusal = claim("S", observed)
    assert claimed is None
    assert refusal.reason == SESSION_COORDINATION_UNAVAILABLE
    assert _state_path().read_bytes() == corrupt, (
        "a registry we could not parse must be left exactly as found for the operator"
    )

    fenced = validate_active_session_lease(owner_lease)
    assert fenced is not None and fenced.reason == SESSION_COORDINATION_UNAVAILABLE


def test_an_unknown_session_is_not_claimable_by_handoff():
    """A handoff moves an OBSERVED owner's lease; it never invents one.

    Without an owner there is nothing to fence, so the caller must fall back to
    an ordinary acquisition rather than have the CAS mint a lease out of an
    observation of nothing.
    """
    acquire("OTHER")
    before = _read_entries(_state_path())

    claimed, refusal = handoff_active_session(
        session_id="S", surface="desktop", metadata={"live_session_id": "live-x"},
        expected_lease_id="0" * 32, expected_generation=1, track_liveness=True,
    )

    assert claimed is None
    assert refusal.reason == SESSION_HANDOFF_STALE
    assert _read_entries(_state_path()) == before


def test_liveness_that_cannot_be_proven_fails_closed():
    """A pid we cannot vouch for is not an idle owner.

    The registry entry names a live pid whose start time does not match, so
    ``_pid_liveness`` cannot answer. Ownership uncertainty has exactly one safe
    answer, and it is not "go ahead".
    """
    acquire("S")
    observed = observe_active_session_owner("S")

    entries = _read_entries(_state_path())
    entries[0]["pid"] = os.getpid()
    entries[0]["process_start_time"] = None
    # An entry that claims our live pid with no start time is provable (alive);
    # blank the probe instead so liveness is genuinely unknowable.
    _write_entries(_state_path(), entries)

    import hermes_cli.active_sessions as active_sessions

    original = active_sessions._pid_liveness

    def unknowable(pid, process_start_time=None, *, lenient=False):
        return None if not lenient else original(pid, process_start_time, lenient=lenient)

    active_sessions._pid_liveness = unknowable
    try:
        claimed, refusal = claim("S", observed)
    finally:
        active_sessions._pid_liveness = original

    assert claimed is None
    assert refusal.reason == SESSION_COORDINATION_UNAVAILABLE


def test_a_dead_owner_leaves_nothing_to_hand_off():
    """A crashed owner is reclaimed by the ordinary acquire path, not by handoff.

    Handoff exists for a LIVE owner. Routing a dead one through it would make
    the CAS the place where crash recovery lives, and the two have different
    safety arguments.
    """
    acquire("S")
    observed = observe_active_session_owner("S")

    entries = _read_entries(_state_path())
    entries[0]["pid"] = 0x7FFFFFFE
    entries[0]["process_start_time"] = 1.0
    _write_entries(_state_path(), entries)

    assert observe_active_session_owner("S") is None, "a dead owner is not an owner"

    claimed, refusal = claim("S", observed)
    assert claimed is None and refusal.reason == SESSION_HANDOFF_STALE

    successor, refusal = acquire("S")
    assert successor is not None, f"ordinary acquisition still reclaims a dead owner: {refusal}"


def test_the_ordinary_refusal_carries_what_a_claimant_needs_and_no_secrets():
    """The 4090 payload is the UI's only input for a safe "Move here".

    It must carry the exact CAS precondition (lease id + generation) and the
    human context (which surface, how long, busy or not) -- and nothing that
    would leak the owner's environment.
    """
    acquire("S", surface="desktop")
    _, refusal = acquire("S", surface="desktop")

    assert refusal.reason == SESSION_NOT_OWNED
    ownership = refusal.ownership
    assert ownership["session_id"] == "S"
    assert ownership["generation"] >= 1
    assert ownership["lease_id"]
    assert ownership["surface"] == "desktop"
    assert ownership["busy"] is False
    assert ownership["handoff_supported"] is True
    assert set(ownership) == {
        "session_id", "lease_id", "generation", "surface", "busy", "age_seconds", "handoff_supported",
    }, "the wire payload is a closed set: no pids, paths, metadata or credentials"


def test_a_fenced_lease_never_validates_again_even_after_the_claimant_releases():
    """The fence is permanent, not "until the registry entry goes away".

    Ordering: A owns gen1 -> the user moves the chat to B (gen2) -> A is fenced
    -> B closes the chat, so its entry is gone. A's in-memory lease object is
    still there, still naming gen1. Reconstructing A as owner from that object
    would resurrect a writer the user explicitly moved away from -- and A's
    view of the transcript is a generation out of date.

    An entry-less slot cannot PROVE the holder is still the owner, so the only
    safe answer is refusal. A is not locked out of the chat forever: its next
    submit holds no lease and takes the ordinary acquisition path, which is a
    fresh lease under the registry lock, not a resurrected one.
    """
    old_lease, _ = acquire("S")
    new_lease, refusal = claim("S", observe_active_session_owner("S"))
    assert new_lease is not None, refusal

    from hermes_cli.active_sessions import release_active_session

    release_active_session(new_lease)
    assert observe_active_session_owner("S") is None, "the claimant let the chat go"

    fenced = validate_active_session_lease(old_lease)
    assert fenced is not None, "a superseded lease must never become authority again"
    assert fenced.reason == SESSION_NOT_OWNED

    assert begin_active_session_turn(old_lease, turn_token="tab-1") is not None, (
        "and it must not be able to open a turn on the strength of a stale generation"
    )
    assert observe_active_session_owner("S") is None, (
        "a refused validation must not write the stale owner back into the registry"
    )


def test_a_fenced_lease_cannot_resurrect_itself_by_rotating_its_session_id():
    """The fence has to hold on the TRANSFER path too, not only on validate/begin-turn.

    Same ordering as the test above -- A owns gen1, the user moves the chat to B
    (gen2), B closes it -- except A's next act is a session-id rotation (a
    compaction, a resumed chat taking a new stored id). Transfer is an upsert for
    a lease whose entry was transiently pruned; for a SUPERSEDED lease it would
    write the fenced writer back into the registry under a fresh id, silently
    undoing the user's move.
    """
    old_lease, _ = acquire("S")
    new_lease, refusal = claim("S", observe_active_session_owner("S"))
    assert new_lease is not None, refusal

    from hermes_cli.active_sessions import release_active_session, transfer_active_session

    release_active_session(new_lease)
    assert active_session_registry_snapshot() == [], "the claimant let the chat go"

    assert transfer_active_session(old_lease, session_id="S-rotated") is False, (
        "a superseded lease must not be able to carry its slot to a new session id"
    )
    assert active_session_registry_snapshot() == [], (
        "a refused transfer must not recreate an owner the user moved away from"
    )
    assert old_lease.session_id == "S", "a refused transfer must not retarget the lease"
    assert observe_active_session_owner("S-rotated") is None


def test_a_fence_survives_more_than_the_old_global_pruning_cap():
    """Unrelated handoffs must never evict another chat's permanent fence.

    A stale lease can outlive hundreds of later handoffs in another process. If
    S's high-water mark is pruned, transfer's entry-less upsert can resurrect A
    at generation 1 after the user already moved S to generation 2.
    """
    from hermes_cli.active_sessions import release_active_session, transfer_active_session

    old_lease, refusal = acquire("S")
    assert old_lease is not None, refusal
    claimant, refusal = claim("S", observe_active_session_owner("S"))
    assert claimant is not None, refusal
    release_active_session(claimant)

    for index in range(300):
        session_id = f"other-{index}"
        owner, refusal = acquire(session_id)
        assert owner is not None, refusal
        moved, refusal = claim(session_id, observe_active_session_owner(session_id))
        assert moved is not None, refusal
        release_active_session(moved)

    assert transfer_active_session(old_lease, session_id="S-rotated") is False
    assert observe_active_session_owner("S") is None
    assert observe_active_session_owner("S-rotated") is None

    reacquired, refusal = acquire("S")
    assert reacquired is not None, refusal
    assert reacquired.generation > claimant.generation


def test_an_entry_less_slot_is_reacquirable_but_only_as_a_new_lease():
    """The other half of the rule: refusal is not a lockout.

    A fenced runtime may own the chat again -- by ACQUIRING it when nobody does,
    under the registry lock, with a fresh lease id and a fresh generation. What
    it may never do is keep writing on the old one.
    """
    old_lease, _ = acquire("S", live_id="tab-1")
    new_lease, _ = claim("S", observe_active_session_owner("S"))

    from hermes_cli.active_sessions import release_active_session

    release_active_session(new_lease)

    reacquired, refusal = acquire("S", live_id="tab-1")
    assert reacquired is not None, f"an unowned chat must be acquirable: {refusal}"
    assert reacquired.lease_id != old_lease.lease_id
    assert reacquired.generation > old_lease.generation, (
        "the new lease must not reuse the superseded generation"
    )
    assert validate_active_session_lease(reacquired) is None
    assert validate_active_session_lease(old_lease).reason == SESSION_NOT_OWNED
