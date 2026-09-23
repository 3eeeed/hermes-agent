"""Isolated Debate results are proposals; only the parent may commit them."""
import threading
import io
import json
from types import SimpleNamespace

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.prompt_execution_modes import build_execution_spec
from hermes_cli.active_sessions import try_acquire_active_session
from hermes_state import SessionDB


def test_isolated_submit_child_proposal_parent_commit_one_completion(monkeypatch, tmp_path):
    from tui_gateway import mode_model_call
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    key, sid = "isolated-full-stored", "isolated-full-runtime"
    db.create_session(key, source="desktop")
    lease, refusal = try_acquire_active_session(
        session_id=key, surface="desktop", config={}, metadata={"live_session_id": sid},
        track_liveness=True)
    assert lease is not None, refusal
    options = {"side_a": {"provider": "openrouter", "model": "a"},
               "side_b": {"provider": "openrouter", "model": "b"},
               "judge": {"provider": "openrouter", "model": "judge"}, "rounds": 1}
    spec = build_execution_spec("debate", options)
    parent = {"session_key": key, "source": "desktop", "history": [],
              "history_version": 0, "history_lock": threading.RLock(),
              "running": False, "agent": None, "_compute_host_active": True,
              "_execution_mode": "debate", "_execution_spec": spec,
              "active_session_lease": lease, "attached_images": [], "cols": 80}
    child = {"session_key": key, "history_lock": threading.RLock(), "running": False,
             "_turn_cancel_requested": False, "agent": SimpleNamespace(_session_messages=[])}
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    monkeypatch.setattr(host, "_ensure_server_session", lambda *a: child)
    monkeypatch.setattr(server, "_install_borrowed_lease", lambda *a: None)
    calls = []
    def advisory(route, prompt, max_tokens):
        calls.append(route.model)
        return "judge answer" if route.model == "judge" else route.model
    monkeypatch.setattr(mode_model_call, "call_model", advisory)
    events = []
    monkeypatch.setattr(server, "_emit", lambda *a: events.append(a))
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a: False)
    class InlineSupervisor:
        def submit_turn(self, frame, on_complete):
            host._run_real_turn(frame)
            terminal = [json.loads(line) for line in out.getvalue().splitlines()
                        if json.loads(line).get("type") in {"turn.end", "turn.error"}][-1]
            on_complete(terminal)
            # A newer attachment may arrive after the child settles but before
            # the original submit_turn call unwinds; it belongs to the NEXT turn.
            parent["attached_images"].append("/tmp/next-turn.png")
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: InlineSupervisor())
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **kw: True)
    with server._sessions_lock:
        server._sessions[sid] = parent
    try:
        result = server.handle_request({"id": 1, "method": "prompt.submit", "params": {
            "session_id": sid, "text": "Question?", "client_send_id": "isolated-debate-send",
            "execution_mode": "debate", "mode_options": options}})
        assert result["result"]["status"] == "streaming"
        assert calls == ["a", "b", "judge"]
        assert [(m["role"], m["content"]) for m in db.get_messages_as_conversation(key)] == [
            ("user", "Question?"), ("assistant", "judge answer")]
        assert [e[2]["text"] for e in events if e[0] == "message.complete"] == ["judge answer"]
        assert parent["history"] == db.get_messages_as_conversation(key, include_row_ids=True)
        assert parent["attached_images"] == ["/tmp/next-turn.png"]
    finally:
        with server._sessions_lock:
            server._sessions.pop(sid, None)
        lease.release()
        host.close()
        db.close()


def test_child_releases_own_running_state_before_terminal_callback_replays_next_turn(monkeypatch):
    import tui_gateway.mode_model_call as model_call
    spec = build_execution_spec("debate", {
        "side_a": {"provider": "test", "model": "a"},
        "side_b": {"provider": "test", "model": "b"},
        "judge": {"provider": "test", "model": "judge"}, "rounds": 1})
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    child = {"session_key": "stored", "history_lock": threading.RLock(), "running": False,
             "history": [], "history_version": 0, "_turn_cancel_requested": False,
             "agent": SimpleNamespace(_session_messages=[])}
    real_ensure = host._ensure_server_session
    monkeypatch.setattr(host, "_ensure_server_session", lambda *a: child)
    monkeypatch.setitem(server._sessions, "child-barrier", child)
    monkeypatch.setattr(server, "_install_borrowed_lease", lambda *a: None)
    monkeypatch.setattr(model_call, "call_model", lambda route, *a: route.model)
    reply = host._reply
    observed = []
    def terminal_barrier(kind, sid, request_id, **kwargs):
        if kind == "turn.end":
            observed.append(child["running"])
            # A synchronous parent callback commits and sends the next canonical
            # history before this child's turn handler has returned.
            rows = [{"role": "user", "content": "Question?", "_row_id": 17},
                    {"role": "assistant", "content": "judge", "_row_id": 18}]
            refreshed = real_ensure(server, {
                "sid": sid, "session_key": "stored", "history_version": 1, "history": rows})
            assert refreshed["history"] == rows
            # A successor now owns running; the old turn's finally cannot clear it.
            child["running"] = True
            child["_isolated_debate_turn_id"] = "next"
        return reply(kind, sid, request_id, **kwargs)
    monkeypatch.setattr(host, "_reply", terminal_barrier)
    try:
        host._run_real_turn({"sid": "child-barrier", "request_id": "turn-1", "turn_id": "turn-1",
                             "debate_nonce": "secret", "text": "Question?", "session_key": "stored",
                             "history_version": 0, "execution_mode": "debate",
                             "execution_spec": spec.as_wire(),
                             "active_session_lease": {"session_id": "stored", "lease_id": "lease"},
                             "submit_user_row": {"role": "user", "content": "Question?",
                                                 "_row_id": 17, "_db_persisted": True}})
        assert observed == [False]
        assert child["running"] is True
        assert child["_isolated_debate_turn_id"] == "next"
    finally:
        host.close()


def test_parent_rejects_wrong_child_result_without_persisting(monkeypatch):
    session = {"session_key": "stored", "history_lock": threading.RLock(), "running": True,
               "history": [], "history_version": 0, "_compute_host_turn_id": "turn-1",
               "_submit_user_row": {"role": "user", "content": "Question?", "_row_id": 17,
                                    "_db_persisted": True},
               "_compute_host_debate_commit": {"turn_id": "turn-1", "nonce": "secret",
                                                "user_row_id": 17, "history_version": 0,
                                                "text": "Question?"},
               "active_session_lease": SimpleNamespace(enabled=True, released=False,
                                                       session_id="stored", lease_id="lease")}
    writes = []
    import tui_gateway.external_result_persistence as persistence
    monkeypatch.setattr(persistence, "commit_external_assistant_result", lambda *a, **k: writes.append(1))
    monkeypatch.setattr(server, "_finish_session_turn", lambda *a: None)
    monkeypatch.setattr(server, "_release_deferred_active_session_lease", lambda *a: None)
    monkeypatch.setattr(server, "_emit", lambda *a: None)
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a: None)
    monkeypatch.setattr(server, "_compute_host_session_info", lambda *a: {})
    frame = {"type": "turn.end", "sid": "sid", "request_id": "turn-1", "turn_id": "turn-1",
             "debate_result": {"answer": "Judge", "user_row_id": 999, "history_version": 0}}
    server._on_compute_host_turn_done("rid", "sid", session, frame)
    assert writes == []
    assert session.get("_closing") is True


def test_child_proposes_only_judge_answer_without_persistence(monkeypatch):
    import tui_gateway.mode_model_call as model_call
    spec = build_execution_spec("debate", {
        "side_a": {"provider": "test", "model": "a"},
        "side_b": {"provider": "test", "model": "b"},
        "judge": {"provider": "test", "model": "judge"}, "rounds": 1})
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    session = {"session_key": "stored", "history_lock": threading.RLock(), "running": False,
               "_turn_cancel_requested": False}
    monkeypatch.setattr(host, "_ensure_server_session", lambda *a: session)
    monkeypatch.setattr(server, "_install_borrowed_lease", lambda *a: None)
    monkeypatch.setattr(model_call, "call_model", lambda route, *a: route.model)
    try:
        host._run_real_turn({"sid": "sid", "request_id": "turn-1", "turn_id": "turn-1",
                             "debate_nonce": "secret", "text": "Question?", "session_key": "stored",
                             "history_version": 0, "execution_mode": "debate",
                             "execution_spec": spec.as_wire(),
                             "active_session_lease": {"session_id": "stored", "lease_id": "lease"},
                             "submit_user_row": {"role": "user", "content": "Question?",
                                                 "_row_id": 17, "_db_persisted": True}})
        end = json.loads(out.getvalue().splitlines()[-1])
        assert end["type"] == "turn.end"
        assert end["debate_result"] == {"turn_id": "turn-1", "nonce": "secret",
                                        "user_row_id": 17, "history_version": 0,
                                        "answer": "judge"}
    finally:
        host.close()


def test_parent_commits_and_verifies_before_completion_and_lease_release(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    key, sid = "debate-stored", "debate-runtime"
    db.create_session(key, source="desktop")
    lease, refusal = try_acquire_active_session(
        session_id=key, surface="desktop", config={}, metadata={"live_session_id": sid},
        track_liveness=True)
    assert lease is not None, refusal
    events = []
    session = {"session_key": key, "source": "desktop", "history": [],
               "history_version": 0, "history_lock": threading.RLock(),
               "running": True, "agent": None, "_compute_host_active": True,
               "active_session_lease": lease, "_compute_host_turn_id": "turn-1"}
    try:
        assert server._reserve_session_turn(sid, session) is None
        server._persist_submit_user_row(session, "Question?", None)
        row_id = session["_submit_user_row"]["_row_id"]
        session["_compute_host_debate_commit"] = {
            "turn_id": "turn-1", "nonce": "secret", "user_row_id": row_id,
            "history_version": 0, "text": "Question?"}
        original_finish = server._finish_session_turn
        def finish(*args):
            assert any(e[0] == "message.complete" and e[2]["status"] == "complete" for e in events)
            original_finish(*args)
        monkeypatch.setattr(server, "_finish_session_turn", finish)
        def emit(kind, runtime, payload):
            if kind == "message.complete":
                assert not lease.released
                assert [(r["role"], r["content"]) for r in db.get_messages_as_conversation(key)] == [
                    ("user", "Question?"), ("assistant", "judge")]
            events.append((kind, runtime, payload))
        monkeypatch.setattr(server, "_emit", emit)
        monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a: False)
        server._on_compute_host_turn_done("rid", sid, session, {
            "type": "turn.end", "sid": sid, "request_id": "turn-1",
            "debate_result": {"turn_id": "turn-1", "nonce": "secret",
                              "user_row_id": row_id, "history_version": 0, "answer": "judge"}})
        assert session["history"] == db.get_messages_as_conversation(key, include_row_ids=True)
        assert session["history_version"] == 1
        assert session.get("_closing") is not True
    finally:
        lease.release()
        db.close()


def test_child_death_never_publishes_a_judge_answer(monkeypatch):
    from tui_gateway.host_supervisor import HostSupervisor
    import tui_gateway.external_result_persistence as persistence
    session = {"session_key": "stored", "history_lock": threading.RLock(),
               "running": True, "history": [], "_compute_host_debate_commit": {
                   "turn_id": "turn", "nonce": "secret", "user_row_id": 17,
                   "history_version": 0, "text": "Question?"}}
    events = []
    monkeypatch.setattr(persistence, "commit_external_assistant_result",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected write")))
    monkeypatch.setattr(server, "_emit", lambda kind, sid, payload: events.append((kind, payload)))
    monkeypatch.setattr(server, "_finish_session_turn", lambda *a: None)
    monkeypatch.setattr(server, "_release_deferred_active_session_lease", lambda *a: None)
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a: False)
    supervisor = HostSupervisor(rpc_sink=lambda *a: True)
    supervisor._pending_turns["turn"] = (
        "sid", lambda frame: server._on_compute_host_turn_done("rid", "sid", session, frame))
    supervisor._fail_pending_turns(reason="crash", message="child died")
    assert session["_closing"] is True
    assert not any(kind == "message.complete" and payload.get("status") == "complete"
                   for kind, payload in events)


def test_handoff_before_parent_commit_refuses_answer(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    key, sid = "handoff-stored", "handoff-runtime"
    db.create_session(key, source="desktop")
    lease, refusal = try_acquire_active_session(session_id=key, surface="desktop", config={},
        metadata={"live_session_id": sid}, track_liveness=True)
    assert lease is not None, refusal
    session = {"session_key": key, "source": "desktop", "history": [],
               "history_version": 0, "history_lock": threading.RLock(),
               "running": True, "agent": None, "_compute_host_active": True,
               "active_session_lease": lease}
    successor = None
    try:
        assert server._reserve_session_turn(sid, session) is None
        server._persist_submit_user_row(session, "Question?", None)
        row = session["_submit_user_row"]["_row_id"]
        lease.release()
        successor, refusal = try_acquire_active_session(session_id=key, surface="desktop",
            config={}, metadata={"live_session_id": "successor"}, track_liveness=True)
        assert successor is not None, refusal
        from tui_gateway.external_result_persistence import commit_external_assistant_result
        import pytest
        with pytest.raises(RuntimeError, match="admitted gateway turn reservation"):
            commit_external_assistant_result(sid, session, "Question?", "judge", history_version=0)
        assert row > 0
        assert [r["role"] for r in db.get_messages_as_conversation(key)] == ["user"]
    finally:
        server._finish_session_turn(sid, session)
        if successor is not None:
            successor.release()
        lease.release()
        db.close()


def test_child_resumes_from_parent_canonical_history_after_debate(monkeypatch):
    agent = SimpleNamespace(session_id="stored", _session_messages=[])
    child = {"session_key": "stored", "history_lock": threading.RLock(),
             "history": [], "history_version": 0, "running": False,
             "agent": agent, "transport": None}
    monkeypatch.setitem(server._sessions, "debate-refresh", child)
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    rows = [{"role": "user", "content": "Question?", "_row_id": 17},
            {"role": "assistant", "content": "judge", "_row_id": 18}]
    try:
        resumed = host._ensure_server_session(server, {"sid": "debate-refresh",
            "session_key": "stored", "history": rows, "history_version": 1})
        assert resumed["history"] == agent._session_messages == rows
        assert resumed["history_version"] == 1
    finally:
        server._sessions.pop("debate-refresh", None)
        host.close()
