"""External answers join the admitted gateway turn without faking agent.run_conversation."""
import os
import threading
from types import SimpleNamespace

import pytest

from hermes_cli.active_sessions import try_acquire_active_session
from hermes_state import SessionDB
from hermes_state_pidns import holder_namespace_token
from tui_gateway import server
from tui_gateway.external_result_persistence import commit_external_assistant_result


def test_external_result_refuses_cancel_and_history_change_before_db_write(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    key, sid = "external-refusal-session", "external-refusal-runtime"
    db.create_session(key, source="desktop")
    lease, refusal = try_acquire_active_session(
        session_id=key, surface="desktop", config={}, metadata={"live_session_id": sid},
        track_liveness=True)
    assert lease is not None, refusal
    agent = SimpleNamespace(session_id=key, _session_messages=[])
    session = {"session_key": key, "source": "desktop", "history": [], "history_version": 0,
               "history_lock": threading.RLock(), "running": True,
               "agent": agent, "active_session_lease": lease}
    try:
        assert server._ensure_active_session_slot(sid, session) is None
        server._persist_submit_user_row(session, "Question?", None)
        session["_turn_cancel_requested"] = True
        with pytest.raises(RuntimeError, match="cancelled"):
            commit_external_assistant_result(sid, session, "Question?", "Answer.", history_version=0)
        session.pop("_turn_cancel_requested")
        session["history_version"] = 1
        with pytest.raises(RuntimeError, match="history changed"):
            commit_external_assistant_result(sid, session, "Question?", "Answer.", history_version=0)
        assert [(r["role"], r["content"]) for r in db.get_messages_as_conversation(key)] == [
            ("user", "Question?")]
        assert session["history"] == agent._session_messages == []
        session["history_version"] = 0
        lease.release()
        successor, refusal = try_acquire_active_session(
            session_id=key, surface="desktop", config={},
            metadata={"live_session_id": "other-runtime"}, track_liveness=True)
        assert successor is not None, refusal
        try:
            with pytest.raises(RuntimeError, match="session.s active slot"):
                commit_external_assistant_result(sid, session, "Question?", "Answer.", history_version=0)
            assert len(db.get_messages_as_conversation(key)) == 1
        finally:
            successor.release()
    finally:
        lease.release()
        db.close()


def test_external_result_db_failure_leaves_history_unpublished_and_retry_commits(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    key, sid = "external-db-failure-session", "external-db-failure-runtime"
    db.create_session(key, source="desktop")
    lease, refusal = try_acquire_active_session(
        session_id=key, surface="desktop", config={}, metadata={"live_session_id": sid},
        track_liveness=True)
    assert lease is not None, refusal
    agent = SimpleNamespace(session_id=key, _session_messages=[])
    session = {"session_key": key, "source": "desktop", "history": [], "history_version": 0,
               "history_lock": threading.RLock(), "running": True,
               "agent": agent, "active_session_lease": lease}
    try:
        assert server._ensure_active_session_slot(sid, session) is None
        server._persist_submit_user_row(session, "Question?", None)
        original = db.append_external_assistant_result
        def fail_write(*args, **kwargs):
            raise OSError("write failed")
        monkeypatch.setattr(db, "append_external_assistant_result", fail_write)
        with pytest.raises(OSError, match="write failed"):
            commit_external_assistant_result(sid, session, "Question?", "Answer.", history_version=0)
        assert session["history"] == agent._session_messages == []
        assert len(db.get_messages_as_conversation(key)) == 1
        monkeypatch.setattr(db, "append_external_assistant_result", original)
        assert commit_external_assistant_result(sid, session, "Question?", "Answer.", history_version=0)
        assert session["history"] == agent._session_messages == db.get_messages_as_conversation(
            key, include_row_ids=True)
    finally:
        lease.release()
        db.close()


def test_post_commit_replay_error_refuses_same_turn_retry_without_duplicate(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    key, sid = "external-postcommit-session", "external-postcommit-runtime"
    db.create_session(key, source="desktop")
    lease, refusal = try_acquire_active_session(
        session_id=key, surface="desktop", config={}, metadata={"live_session_id": sid},
        track_liveness=True)
    assert lease is not None, refusal
    agent = SimpleNamespace(session_id=key, _session_messages=[])
    session = {"session_key": key, "source": "desktop", "history": [], "history_version": 0,
               "history_lock": threading.RLock(), "running": True,
               "agent": agent, "active_session_lease": lease}
    try:
        assert server._ensure_active_session_slot(sid, session) is None
        server._persist_submit_user_row(session, "Question?", None)
        original = db.get_messages_as_conversation
        def fail_replay(*args, **kwargs):
            raise OSError("replay unavailable")
        monkeypatch.setattr(db, "get_messages_as_conversation", fail_replay)
        with pytest.raises(OSError, match="replay unavailable"):
            commit_external_assistant_result(sid, session, "Question?", "Answer.", history_version=0)
        assert session["history"] == agent._session_messages == []
        monkeypatch.setattr(db, "get_messages_as_conversation", original)
        with pytest.raises(RuntimeError, match="cancelled or is not running"):
            commit_external_assistant_result(sid, session, "Question?", "different retry", history_version=0)
        rows = db.get_messages_as_conversation(key, include_row_ids=True)
        assert session["history"] == agent._session_messages == []
        assert [(r["role"], r["content"]) for r in rows] == [
            ("user", "Question?"), ("assistant", "Answer.")]
        assert db.get_session(key)["message_count"] == len(rows)
    finally:
        lease.release()
        db.close()


def test_post_commit_replay_failure_quarantines_next_turn_before_agent_runs(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    key, sid = "external-quarantine-session", "external-quarantine-runtime"
    db.create_session(key, source="desktop")
    lease, refusal = try_acquire_active_session(
        session_id=key, surface="desktop", config={}, metadata={"live_session_id": sid},
        track_liveness=True)
    assert lease is not None, refusal
    agent = SimpleNamespace(session_id=key, _session_messages=[])
    session = {"session_key": key, "source": "desktop", "history": [], "history_version": 0,
               "history_lock": threading.RLock(), "running": True,
               "agent": agent, "active_session_lease": lease}
    try:
        assert server._ensure_active_session_slot(sid, session) is None
        server._persist_submit_user_row(session, "Question?", None)
        original = db.get_messages_as_conversation

        def fail_replay(*args, **kwargs):
            raise OSError("replay unavailable")

        monkeypatch.setattr(db, "get_messages_as_conversation", fail_replay)
        with pytest.raises(OSError, match="replay unavailable"):
            commit_external_assistant_result(sid, session, "Question?", "Answer.", history_version=0)
        monkeypatch.setattr(db, "get_messages_as_conversation", original)
        session["running"] = False
        rows = original(key, include_row_ids=True)
        assert [(row["role"], row["content"]) for row in rows] == [
            ("user", "Question?"), ("assistant", "Answer.")]
        assert session["_closing"] is True
        assert session["history"] == agent._session_messages == []

        # The shared admission gate handles ordinary, queued and synthesized turns.
        monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
        session["running"] = True  # an accepted following submit
        admitted = server._admit_prompt_turn(sid, session, "Follow-up?", None, None, None, None)
        assert admitted is None
        assert session["history"] == agent._session_messages == []
    finally:
        lease.release()
        db.close()


def test_quarantined_session_refuses_submit_before_persisting_followup(monkeypatch):
    session = {"_closing": True, "history_lock": threading.RLock(), "running": False}
    monkeypatch.setattr(server, "_sess_nowait", lambda params, rid: (session, None))
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    monkeypatch.setattr(server, "_legacy_group_fence_error", lambda *a: None)
    monkeypatch.setattr(server, "_reattach_refusal", lambda *a: None)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: False)
    def unexpected(*args, **kwargs):
        raise AssertionError("quarantine must refuse before mutation")
    monkeypatch.setattr(server, "_persist_session_row_for_submit", unexpected)
    monkeypatch.setattr(server, "_lock_in_submit_turn", unexpected)
    response = server.handle_request({"id": 1, "method": "prompt.submit",
                                      "params": {"session_id": "quarantined", "text": "Follow-up?"}})
    assert response["error"]
    assert session.get("running") is False


def test_external_result_commits_once_and_replays_after_retry(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    key, sid = "external-result-session", "external-result-runtime"
    db.create_session(key, source="desktop")
    lease, refusal = try_acquire_active_session(
        session_id=key, surface="desktop", config={}, metadata={"live_session_id": sid},
        track_liveness=True)
    assert lease is not None, refusal
    session = {"session_key": key, "source": "desktop", "history": [], "history_version": 0,
               "history_lock": threading.RLock(), "running": True,
               "agent": SimpleNamespace(session_id=key), "active_session_lease": lease}
    holders = []
    acquire = db.acquire_session_turn_lease

    def record_acquire(session_key, holder, **kwargs):
        holders.append(holder)
        return acquire(session_key, holder, **kwargs)

    monkeypatch.setattr(db, "acquire_session_turn_lease", record_acquire)
    try:
        assert server._ensure_active_session_slot(sid, session) is None
        server._persist_submit_user_row(session, "Question?", None)
        assert commit_external_assistant_result(sid, session, "Question?", "Answer.", history_version=0)
        assert holders == [f"pid={os.getpid()}{holder_namespace_token()}:"
                           f"external={threading.get_ident()}:{session['history'][0]['_row_id']}"]
        assert not commit_external_assistant_result(sid, session, "Question?", "Answer.", history_version=0)
        rows = db.get_messages_as_conversation(key, include_row_ids=True)
        assert [(r["role"], r["content"]) for r in rows] == [
            ("user", "Question?"), ("assistant", "Answer.")]
        assert session["history"] == rows
        assert db.get_session(key)["message_count"] == len(rows)
    finally:
        lease.release()
        db.close()
