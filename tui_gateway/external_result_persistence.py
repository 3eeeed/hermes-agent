"""Commit an externally produced answer to an already admitted gateway turn.

Not a substitute for agent.run_conversation: callers own turn admission and completion.
"""
from __future__ import annotations

import os
import threading

from agent.context_compressor import _DB_PERSISTED_MARKER
from hermes_state_pidns import holder_namespace_token


def commit_external_assistant_result(
    sid: str, session: dict, user_text: str, answer: str, *, history_version: int,
) -> bool:
    """Persist once, then publish the exact durable pair to the warm agent/session history.

    Returns False on a repeat of the already published turn. Refusals raise before
    writing; an exception after SQLite commit quarantines the live session so a later
    turn cannot run against an older warm snapshot. Resume from SQLite in a new session.
    The caller must already hold the session's active slot until all its work ends.
    """
    from tui_gateway import server

    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("external assistant answer must be nonempty text")
    key = str(session.get("session_key") or "")
    agent = session.get("agent")
    with session["history_lock"]:
        committed = session.get("_external_result_commit")
        staged = session.get("_submit_user_row")
        if not session.get("running") or session.get("_closing") or session.get("_turn_cancel_requested"):
            raise RuntimeError("external result turn was cancelled or is not running")
        repeat = (staged is None and committed and committed["user_text"] == user_text
                  and int(session.get("history_version", 0)) == history_version + 1
                  and committed["history_version"] == history_version)
        if not repeat and int(session.get("history_version", 0)) != history_version:
            raise RuntimeError("external result history changed before commit")
        if not repeat and (not isinstance(staged, dict) or staged.get("content") != user_text
                or staged.get(_DB_PERSISTED_MARKER) is not True
                or not isinstance(staged.get("_row_id"), int)):
            raise RuntimeError("external result requires this turn's durable submit-time user row")
        if agent is None or getattr(agent, "session_id", None) != key:
            raise RuntimeError("external result session identity changed")
        lease = session.get("active_session_lease")
        if lease is None or getattr(lease, "released", False):
            raise RuntimeError("external result requires the session's active slot")
        refusal = server._ensure_active_session_slot(sid, session)
        if refusal is not None:
            raise RuntimeError(f"external result turn ownership lost: {refusal}")
        if repeat:
            if committed["answer"] != answer:
                raise RuntimeError("external result retry differs from committed answer")
            return False
        with server._session_db(session) as db:
            if db is None:
                raise RuntimeError("external result session database unavailable")
            holder = (f"pid={os.getpid()}{holder_namespace_token()}:"
                      f"external={threading.get_ident()}:{staged['_row_id']}")
            if not db.acquire_session_turn_lease(key, holder, wait_seconds=0,
                                                 should_abort=lambda: bool(session.get("_turn_cancel_requested"))):
                raise RuntimeError("external result durable turn lease unavailable")
            try:
                if session.get("_turn_cancel_requested") or session.get("history_version", 0) != history_version:
                    raise RuntimeError("external result turn changed before database write")
                row_id, canonical = db.append_external_assistant_result(
                    key, user_row_id=staged["_row_id"], content=answer, turn_lease_holder=holder)
                try:
                    # Replay with the actual row metadata, not a synthetic near-match.
                    rows = db.get_messages_as_conversation(key, include_row_ids=True)
                    # Keep the durable lease through the warm publish, so another surface
                    # cannot append between the read and the in-memory installation.
                    user = next((r for r in rows if r.get("_row_id") == staged["_row_id"]), None)
                    assistant = next((r for r in rows if r.get("_row_id") == row_id), None)
                    if (user is None or assistant is None
                            or user.get("role") != "user" or user.get("content") != user_text
                            or assistant.get("role") != "assistant"
                            or assistant.get("content") != canonical):
                        raise RuntimeError("external result committed but replay rows are unavailable")
                    # The parent of an isolated turn can have a metadata-only
                    # mirror: prior normal turns live in the child and SQLite,
                    # not its warm history. Install the complete durable transcript.
                    history = rows
                    if agent is not None:
                        agent._session_messages = history
                    session["history"] = history
                    session["history_version"] = history_version + 1
                    session["_external_result_commit"] = {
                        "user_text": user_text, "answer": canonical, "history_version": history_version}
                    session.pop("_submit_user_row", None)
                    return True
                except Exception:
                    # SQLite already committed. A later turn must not run against the
                    # pre-commit agent snapshot even if this worker finishes normally.
                    # _closing fences the shared admission gate and worker start, including
                    # queued/synthesized turns. Reopen with a fresh session from SQLite.
                    session["_closing"] = True
                    raise
            finally:
                db.release_session_turn_lease(key, holder)
