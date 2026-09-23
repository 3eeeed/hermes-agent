"""Delegate child proposals require a parent-owned durable commit."""
import io
import json
import threading
from types import SimpleNamespace

from hermes_cli.active_sessions import try_acquire_active_session
from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.prompt_execution_modes import build_execution_spec


OPTIONS = {"orchestrator": {"provider": "test", "model": "synth"},
           "workers": [{"lane": "research", "provider": "test", "model": "worker"}],
           "max_iterations": 2}


def test_delegate_child_proposal_commits_exactly_once_before_completion(monkeypatch, tmp_path):
    import tui_gateway.delegate_readonly_lifecycle as lifecycle
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    key, sid = "delegate-stored", "delegate-runtime"
    db.create_session(key, source="desktop")
    lease, refusal = try_acquire_active_session(session_id=key, surface="desktop", config={},
        metadata={"live_session_id": sid}, track_liveness=True)
    assert lease is not None, refusal
    parent = {"session_key": key, "source": "desktop", "history": [], "history_version": 0,
              "history_lock": threading.RLock(), "running": True, "agent": None,
              "_compute_host_active": True, "active_session_lease": lease, "attached_images": [], "cols": 80,
              "_execution_mode": "delegate", "_execution_spec": build_execution_spec("delegate", OPTIONS)}
    child = {"session_key": key, "history_lock": threading.RLock(), "running": False,
             "_turn_cancel_requested": False, "agent": SimpleNamespace(_session_messages=[])}
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    monkeypatch.setattr(host, "_ensure_server_session", lambda *a: child)
    monkeypatch.setattr(server, "_install_borrowed_lease", lambda *a: None)
    monkeypatch.setattr(lifecycle, "run_readonly_delegate_answer",
                        lambda agent, text, options, **kw: ("Synthesis", [{"status": "completed"}]))
    events = []
    def emit(kind, runtime, payload):
        if kind == "message.complete" and payload.get("status") == "complete":
            assert not lease.released
            assert [(r["role"], r["content"]) for r in db.get_messages_as_conversation(key)] == [
                ("user", "Question?"), ("assistant", "Synthesis")]
        events.append((kind, runtime, payload))
    monkeypatch.setattr(server, "_emit", emit)
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a: False)
    class Supervisor:
        def submit_turn(self, frame, on_complete):
            host._run_real_turn(frame)
            terminal = [json.loads(line) for line in out.getvalue().splitlines()
                        if json.loads(line).get("type") in {"turn.end", "turn.error"}][-1]
            assert terminal["type"] == "turn.end", terminal
            assert [(r["role"], r["content"]) for r in db.get_messages_as_conversation(key)] == [("user", "Question?")]
            on_complete(terminal)
            on_complete(terminal)  # duplicate terminal callback cannot publish twice
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: Supervisor())
    with server._sessions_lock:
        server._sessions[sid] = parent
    try:
        assert server._reserve_session_turn(sid, parent) is None
        server._persist_submit_user_row(parent, "Question?", None)
        result = server._submit_prompt_to_compute_host("rid", sid, parent, "Question?",
                                                      execution_spec=parent["_execution_spec"],
                                                      caller_owns_reservation=True)
        assert result["result"]["status"] == "streaming"
        assert [(r["role"], r["content"]) for r in db.get_messages_as_conversation(key)] == [
            ("user", "Question?"), ("assistant", "Synthesis")]
        assert [e[2]["text"] for e in events if e[0] == "message.complete"] == ["Synthesis"]
        assert parent["history"] == db.get_messages_as_conversation(key, include_row_ids=True)
    finally:
        with server._sessions_lock:
            server._sessions.pop(sid, None)
        lease.release()
        host.close()
        db.close()


def test_delegate_malformed_and_dead_proposals_fail_closed(monkeypatch):
    import tui_gateway.external_result_persistence as persistence
    from tui_gateway.host_supervisor import HostSupervisor
    writes, events = [], []
    monkeypatch.setattr(persistence, "commit_external_assistant_result", lambda *a, **k: writes.append(1))
    monkeypatch.setattr(server, "_emit", lambda kind, sid, payload: events.append((kind, payload)))
    monkeypatch.setattr(server, "_finish_session_turn", lambda *a: None)
    monkeypatch.setattr(server, "_release_deferred_active_session_lease", lambda *a: None)
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a: False)
    session = {"session_key": "stored", "history_lock": threading.RLock(), "running": True,
               "history": [], "history_version": 0, "_submit_user_row": {"_row_id": 17},
               "_compute_host_delegate_commit": {"turn_id": "turn", "nonce": "secret",
                                                 "user_row_id": 17, "history_version": 0, "text": "Question?"}}
    server._on_compute_host_turn_done("rid", "sid", session, {
        "type": "turn.end", "sid": "sid", "request_id": "turn", "delegate_result": {
            "turn_id": "turn", "nonce": "wrong", "user_row_id": 17,
            "history_version": 0, "answer": "Wrong"}})
    assert session["_closing"] is True
    session["_closing"] = False
    session["_compute_host_delegate_commit"] = {"turn_id": "turn2", "nonce": "secret",
        "user_row_id": 17, "history_version": 0, "text": "Question?"}
    supervisor = HostSupervisor(rpc_sink=lambda *a: True)
    supervisor._pending_turns["turn2"] = ("sid", lambda frame: server._on_compute_host_turn_done("rid", "sid", session, frame))
    supervisor._fail_pending_turns(reason="crash", message="child died")
    assert session["_closing"] is True
    assert writes == []
    assert not any(kind == "message.complete" and payload.get("status") == "complete" for kind, payload in events)


def test_delegate_cancelled_child_cannot_propose_answer(monkeypatch):
    import tui_gateway.delegate_readonly_lifecycle as lifecycle
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    child = {"session_key": "stored", "history_lock": threading.RLock(), "running": False,
             "_turn_cancel_requested": False, "agent": SimpleNamespace(_session_messages=[])}
    monkeypatch.setattr(host, "_ensure_server_session", lambda *a: child)
    monkeypatch.setattr(server, "_install_borrowed_lease", lambda *a: None)
    def answer(*args, **kwargs):
        child["_turn_cancel_requested"] = True
        return "Too late", []
    monkeypatch.setattr(lifecycle, "run_readonly_delegate_answer", answer)
    try:
        host._run_real_turn({"sid": "sid", "request_id": "turn", "turn_id": "turn",
            "delegate_nonce": "secret", "text": "Question?", "session_key": "stored",
            "history_version": 0, "execution_mode": "delegate",
            "execution_spec": build_execution_spec("delegate", OPTIONS).as_wire(),
            "active_session_lease": {"session_id": "stored", "lease_id": "lease"},
            "submit_user_row": {"role": "user", "content": "Question?",
                                "_row_id": 17, "_db_persisted": True}})
        terminals = [json.loads(line) for line in out.getvalue().splitlines()
                     if json.loads(line).get("type") in {"turn.end", "turn.error"}]
        assert len(terminals) == 1
        assert terminals[0]["type"] == "turn.error"
        assert "delegate_result" not in terminals[0]
    finally:
        host.close()


def test_ambiguous_isolated_dispatch_keeps_parent_quarantined(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    key, sid = "ambiguous-stored", "ambiguous-runtime"
    db.create_session(key, source="desktop")
    lease, refusal = try_acquire_active_session(session_id=key, surface="desktop", config={},
        metadata={"live_session_id": sid}, track_liveness=True)
    assert lease is not None, refusal
    session = {"session_key": key, "source": "desktop", "history": [], "history_version": 0,
               "history_lock": threading.RLock(), "running": True, "agent": None,
               "active_session_lease": lease, "attached_images": [], "cols": 80,
               "_execution_mode": "delegate"}
    spec = build_execution_spec("delegate", OPTIONS)
    callback = []
    class AmbiguousSupervisor:
        def submit_turn(self, frame, on_complete):
            callback.append((frame, on_complete))
            on_complete({"type": "turn.error", "sid": sid, "request_id": frame["request_id"],
                         "reason": "send_failed", "message": "ambiguous flush"})
            raise OSError("ambiguous flush")
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: AmbiguousSupervisor())
    with server._sessions_lock:
        server._sessions[sid] = session
    try:
        assert server._reserve_session_turn(sid, session) is None
        server._persist_submit_user_row(session, "Question?", None)
        response = server._submit_prompt_to_compute_host("rid", sid, session, "Question?",
                                                          execution_spec=spec, caller_owns_reservation=True)
        assert response["error"]["code"] == 5019
        assert session["_closing"] is True
        assert not lease.released
        assert "_compute_host_delegate_commit" not in session
        frame, complete = callback[0]
        complete({"type": "turn.end", "sid": sid, "request_id": frame["request_id"],
                  "delegate_result": {"answer": "Escaped child"}})
        assert [r["role"] for r in db.get_messages_as_conversation(key)] == ["user"]
    finally:
        with server._sessions_lock:
            server._sessions.pop(sid, None)
        server._finish_session_turn(sid, session)
        lease.release()
        db.close()


def test_delegate_handoff_refuses_parent_commit(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    key, sid = "delegate-handoff", "delegate-old"
    db.create_session(key, source="desktop")
    lease, refusal = try_acquire_active_session(session_id=key, surface="desktop", config={},
        metadata={"live_session_id": sid}, track_liveness=True)
    assert lease is not None, refusal
    session = {"session_key": key, "source": "desktop", "history": [], "history_version": 0,
               "history_lock": threading.RLock(), "running": True, "agent": None,
               "_compute_host_active": True, "active_session_lease": lease}
    events = []
    monkeypatch.setattr(server, "_emit", lambda kind, runtime, payload: events.append((kind, payload)))
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a: False)
    successor = None
    try:
        assert server._reserve_session_turn(sid, session) is None
        server._persist_submit_user_row(session, "Question?", None)
        row_id = session["_submit_user_row"]["_row_id"]
        session["_compute_host_delegate_commit"] = {"turn_id": "turn", "nonce": "secret",
            "user_row_id": row_id, "history_version": 0, "text": "Question?"}
        lease.release()
        successor, refusal = try_acquire_active_session(session_id=key, surface="desktop",
            config={}, metadata={"live_session_id": "delegate-new"}, track_liveness=True)
        assert successor is not None, refusal
        server._on_compute_host_turn_done("rid", sid, session, {
            "type": "turn.end", "sid": sid, "request_id": "turn", "delegate_result": {
                "turn_id": "turn", "nonce": "secret", "user_row_id": row_id,
                "history_version": 0, "answer": "Unowned answer"}})
        assert session["_closing"] is True
        assert [r["role"] for r in db.get_messages_as_conversation(key)] == ["user"]
        assert not any(kind == "message.complete" and payload.get("status") == "complete"
                       for kind, payload in events)
    finally:
        server._finish_session_turn(sid, session)
        if successor is not None:
            successor.release()
        lease.release()
        db.close()
