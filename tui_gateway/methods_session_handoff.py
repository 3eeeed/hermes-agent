"""``session.handoff.claim`` — the one sanctioned way through the per-session write fence.

Two Desktop windows on two machines can reach one remote Hermes profile (each ownershipId
spawns its own ``hermes serve --isolated``), so they are separate PROCESSES sharing one
``runtime/active_sessions.json``. Opening a chat on the second device reads history fine and
every submit is refused ``SESSION_NOT_OWNED``: correct, but there was no way to say "I am
here now" short of starting a new chat.

This handler is that way, and it is deliberately narrow:

* **User-invoked only.** Nothing on the resume/open/activate path claims. The refusal stays
  the default; a person has to ask for the move.
* **Exact compare-and-swap.** The claimant echoes the ``(lease_id, generation)`` it was
  refused with. A stale or replayed observation loses (``SESSION_HANDOFF_STALE``), so two
  devices acting on the same refusal cannot both win.
* **Owner quiescence is proven, not assumed.** The owner records its turn boundaries in its
  own registry entry under the registry's file lock, so a busy owner is refused
  (``SESSION_HANDOFF_BUSY``) with nothing interrupted and nothing transferred.
* **Fail closed.** An unreadable registry or unprovable liveness refuses and leaves the file
  byte-for-byte as found (``SESSION_COORDINATION_UNAVAILABLE``).

Fencing itself lives one layer down (``hermes_cli/active_sessions.py``): the committed
handoff advances the generation, and every mutation boundary re-validates, so the previous
runtime's still-referenced lease object stops being authority the moment this returns.
"""

from __future__ import annotations

import logging

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped

logger = logging.getLogger(__name__)


def _ownership_error(rid, refusal):
    """One error shape for every handoff refusal: 4090 + the machine reason, plus the FRESH
    ownership block when there is one, so the client can re-offer the move without a round trip."""
    data = {"reason": getattr(refusal, "reason", "") or ""}
    if (ownership := getattr(refusal, "ownership", None)):
        data["ownership"] = ownership
    return _err(rid, 4090, str(refusal), data)


def _resolve_stale_claim(rid, sid: str, session: dict, key: str, refusal):
    """A CAS that missed is either "the owner let go" or "somebody else got there first".

    Returns an RPC response dict when the question is settled here, else the refusal to
    report (re-stamped with the CURRENT owner so the client can offer the move again
    without another round trip).
    """
    from hermes_cli.active_sessions import (
        ActiveSessionRefusal, ActiveSessionRegistryError, SESSION_HANDOFF_STALE,
        observe_active_session_owner,
    )

    try:
        owner_now = observe_active_session_owner(key, registry_home=session.get("profile_home"))
    except ActiveSessionRegistryError as exc:
        logger.warning("session.handoff.claim could not re-observe %s: %s", key, exc)
        return _err(rid, 4090, _SESSION_OWNERSHIP_UNAVAILABLE,
                    {"reason": "SESSION_COORDINATION_UNAVAILABLE"})
    if owner_now is not None:
        # A DIFFERENT live owner: the user's evidence is simply out of date. Do not fall
        # through to an ordinary acquisition — that would report "someone owns this" for
        # what is really "re-observe and ask again", and the two call for different UI.
        return ActiveSessionRefusal(str(refusal), SESSION_HANDOFF_STALE, owner_now.as_wire())
    # Nobody owns it: the owner closed the chat (or its backend exited) between the refusal
    # the user clicked and this call. Nothing to fence, so take the ordinary path rather than
    # let the CAS mint a lease from an observation of nothing; it keeps its own fail-closed
    # refusal if an owner reappears inside this window.
    fresh, limit_message = _claim_active_session_slot(
        key, live_session_id=sid, surface=_session_source(session),
        profile_home=session.get("profile_home"))
    if fresh is None:
        return _ownership_error(rid, limit_message or refusal)
    _attach_lease(session, fresh)
    logger.info("Session %s moved here by acquiring a released slot (sid=%s)", key, sid)
    return _ok(rid, {"status": "claimed", "session_id": sid, "stored_session_id": key,
                     "generation": int(getattr(fresh, "generation", 0) or 0)})


@method("session.handoff.claim")
@_profile_scoped
def _(rid, params: dict) -> dict:
    from hermes_cli.active_sessions import (
        ActiveSessionRegistryError, SESSION_HANDOFF_STALE, handoff_active_session,
        validate_active_session_lease,
    )

    sid = str(params.get("session_id") or "")
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    key = str(session.get("session_key") or "")
    if not key:
        # No stored id yet: an unsaved draft has no identity to own, so there is nothing to
        # take over. Claiming one would be a no-op that reads as success to the UI.
        return _err(rid, 4004, "this chat has no stored session to move yet")

    lease = session.get("active_session_lease")
    if lease is not None and validate_active_session_lease(lease) is None:
        return _ok(rid, {"status": "already_owner", "session_id": sid, "stored_session_id": key,
                         "generation": int(getattr(lease, "generation", 0) or 0)})

    expected_lease_id = str(params.get("expected_lease_id") or "")
    expected_generation = params.get("expected_generation")
    if not expected_lease_id or not isinstance(expected_generation, int) or isinstance(expected_generation, bool):
        return _err(rid, 4004, "expected_lease_id and expected_generation are required to move a chat")

    try:
        claimed, refusal = handoff_active_session(
            session_id=key, surface=_session_source(session),
            metadata=_lease_metadata(sid), registry_home=session.get("profile_home"),
            expected_lease_id=expected_lease_id, expected_generation=expected_generation,
            track_liveness=str(_session_source(session) or "").strip().lower() == "desktop")
    except ActiveSessionRegistryError as exc:
        logger.warning("session.handoff.claim could not prove ownership of %s: %s", key, exc)
        return _err(rid, 4090, _SESSION_OWNERSHIP_UNAVAILABLE, {"reason": "SESSION_COORDINATION_UNAVAILABLE"})

    if claimed is None and getattr(refusal, "reason", "") == SESSION_HANDOFF_STALE:
        refusal = _resolve_stale_claim(rid, sid, session, key, refusal)
        if isinstance(refusal, dict):  # the fallback acquisition succeeded (or failed hard)
            return refusal

    if claimed is None:
        return _ownership_error(rid, refusal)

    _attach_lease(session, claimed)
    return _ok(rid, {"status": "claimed", "session_id": sid, "stored_session_id": key,
                     "generation": int(claimed.generation)})


def register(server) -> None:
    """Rebind this module's handlers onto the server namespace."""
    bind_module(globals(), server, skip=("_",))
