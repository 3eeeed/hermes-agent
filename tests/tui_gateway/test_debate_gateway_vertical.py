"""Unsupported Debate must not mutate a durable desktop conversation."""
import threading
import pytest
from types import SimpleNamespace

from hermes_state import SessionDB
from hermes_cli.active_sessions import try_acquire_active_session
from tui_gateway import server
from tui_gateway.prompt_execution_modes import DebateOptions


def test_prompt_submit_inline_debate_writes_one_judge_answer(monkeypatch, tmp_path):
    from tui_gateway import mode_model_call
    from tui_gateway.prompt_execution_modes import build_execution_spec
    db = SessionDB(db_path=tmp_path / "state.db")
    key, sid = "debate-submit-stored", "debate-submit-runtime"
    db.create_session(key, source="desktop")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    lease, refusal = try_acquire_active_session(
        session_id=key, surface="desktop", config={}, metadata={"live_session_id": sid},
        track_liveness=True)
    assert lease is not None, refusal
    agent = SimpleNamespace(session_id=key, _session_messages=[],
                            interim_assistant_callback=None,
                            run_conversation=lambda *a, **kw: pytest.fail("main LLM ran"),
                            clear_interrupt=lambda: None)
    session = {"session_key": key, "source": "desktop", "history": [],
               "history_lock": threading.RLock(), "history_version": 0,
               "running": False, "agent": agent, "agent_ready": threading.Event(),
               "active_session_lease": lease,
               "attached_images": [], "cols": 80}
    session["agent_ready"].set()
    completed = threading.Event()
    events = []
    def capture_event(*args):
        events.append(args)
        if args[0] == "message.complete":
            completed.set()
    monkeypatch.setattr(server, "_emit", capture_event)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *a: None)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **kw: False)
    monkeypatch.setattr(server, "_prepare_turn_input", lambda sid, sess, st, text, images:
                        (text, text, 80, None))
    monkeypatch.setattr(server, "_release_turn_scopes", lambda *a: None)
    monkeypatch.setattr(server, "_goal_followup_after_turn", lambda *a: None)
    monkeypatch.setattr(server, "_after_complete_turn", lambda *a: None)
    monkeypatch.setattr(server, "_publish_session_control_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_emit_settled_session_info", lambda *a: None)
    calls = []
    def advisory(route, prompt, max_tokens):
        calls.append(route.model)
        return "Final judge." if route.model == "judge" else route.model + " argument"
    monkeypatch.setattr(mode_model_call, "call_model", advisory)
    options = {"side_a": {"provider": "openrouter", "model": "a"},
               "side_b": {"provider": "openrouter", "model": "b"},
               "judge": {"provider": "openrouter", "model": "judge"}, "rounds": 1}
    with server._sessions_lock:
        server._sessions[sid] = session
    try:
        response = server.handle_request({"id": 1, "method": "prompt.submit", "params": {
            "session_id": sid, "text": "Question?",
            "execution_mode": "debate", "mode_options": options}})
        assert response["result"]["status"] == "streaming"
        assert completed.wait(5), "Debate did not emit a terminal completion"
        assert calls == ["a", "b", "judge"]
        assert [(m["role"], m["content"]) for m in db.get_messages_as_conversation(key)] == [
            ("user", "Question?"), ("assistant", "Final judge.")]
        assert [e[2]["text"] for e in events if e[0] == "message.complete"] == ["Final judge."]
    finally:
        with server._sessions_lock:
            server._sessions.pop(sid, None)
        if session.get("active_session_lease"):
            session["active_session_lease"].release()
        db.close()


def test_deferred_debate_worker_forwards_immutable_spec(monkeypatch):
    from tui_gateway.prompt_execution_modes import build_execution_spec
    spec = build_execution_spec("debate", {
        "side_a": {"provider": "openrouter", "model": "a"},
        "side_b": {"provider": "openrouter", "model": "b"},
        "judge": {"provider": "openrouter", "model": "judge"}, "rounds": 1})
    session = {"history_lock": threading.RLock(), "running": True,
               "_execution_mode": "debate", "_execution_spec": spec}
    calls = []
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *a: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: calls.append(k))
    server._run_after_agent_ready(1, "runtime", session, "question", None, None, None,
                                  execution_spec=spec)
    assert [c["execution_spec"] for c in calls] == [spec]


def test_admitted_debate_turn_invokes_judge_path_not_normal_agent(monkeypatch):
    from tui_gateway.prompt_execution_modes import build_execution_spec
    spec = build_execution_spec("debate", {
        "side_a": {"provider": "openrouter", "model": "a"},
        "side_b": {"provider": "openrouter", "model": "b"},
        "judge": {"provider": "openrouter", "model": "judge"}, "rounds": 1})
    agent = SimpleNamespace(session_id="debate-route-session", interim_assistant_callback=None,
                            run_conversation=lambda *a, **k: pytest.fail("normal LLM ran"))
    session = {"session_key": agent.session_id, "agent": agent, "history": [],
               "history_lock": threading.RLock(), "history_version": 0, "running": True,
               "_execution_mode": "debate", "_execution_spec": spec}
    called, events = [], []
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a: True)
    monkeypatch.setattr(server, "_admit_prompt_turn", lambda *a, **kw: ([], agent))
    monkeypatch.setattr(server, "_record_turn_marker", lambda *a, **k: "marker")
    monkeypatch.setattr(server, "_prepare_turn_input", lambda *a: ("Question?", "Question?", 80, None))
    def debate(sid, sess, st, prompt, user_text, options, **kwargs):
        called.append((prompt, user_text, options.rounds))
        st.result = {"final_response": "Judge answer.", "messages": []}
    monkeypatch.setattr(server, "_invoke_debate", debate)
    monkeypatch.setattr(server, "_invoke_agent", lambda *a, **k: pytest.fail("normal path ran"))
    monkeypatch.setattr(server, "_complete_turn_payload", lambda sess, st, note, cols:
                        ({"text": st.result["final_response"], "status": "complete"},
                         st.result["final_response"], "complete"))
    monkeypatch.setattr(server, "_release_turn_scopes", lambda *a: None)
    monkeypatch.setattr(server, "_after_complete_turn", lambda *a: None)
    monkeypatch.setattr(server, "_goal_followup_after_turn", lambda *a: None)
    monkeypatch.setattr(server, "_publish_session_control_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(server, "_emit_settled_session_info", lambda *a: None)
    monkeypatch.setattr(server, "_emit", lambda *a: events.append(a))
    assert server._run_prompt_submit("req", "runtime", session, "Question?", execution_spec=spec)
    session["_run_thread"].join(timeout=5)
    assert called == [("Question?", "Question?", 1)]
    assert [e[2]["text"] for e in events if e[0] == "message.complete"] == ["Judge answer."]


def test_debate_worker_commits_judge_once_without_main_agent(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    key, sid = "debate-worker-stored", "debate-worker-runtime"
    db.create_session(key, source="desktop")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    lease, refusal = try_acquire_active_session(
        session_id=key, surface="desktop", config={}, metadata={"live_session_id": sid},
        track_liveness=True)
    assert lease is not None, refusal
    def main_model(*args, **kwargs):
        raise AssertionError("Debate must never invoke the main agent")
    agent = SimpleNamespace(session_id=key, _session_messages=[], run_conversation=main_model)
    session = {"session_key": key, "source": "desktop", "history": [], "history_version": 0,
               "history_lock": threading.RLock(), "running": True,
               "agent": agent, "active_session_lease": lease}
    routes = []
    def advisory(route, prompt, max_tokens):
        routes.append((route.model, prompt, max_tokens))
        return "Judge answer." if route.model == "judge" else route.model + " argument"
    options = DebateOptions.model_validate({
        "side_a": {"provider": "openrouter", "model": "a"},
        "side_b": {"provider": "openrouter", "model": "b"},
        "judge": {"provider": "openrouter", "model": "judge"}, "rounds": 1,
    })
    try:
        assert server._ensure_active_session_slot(sid, session) is None
        server._persist_submit_user_row(session, "Question?", None)
        st = SimpleNamespace(agent=agent, history_version=0, result=None)
        server._invoke_debate(sid, session, st, "Question?", "Question?", options,
                              model_call=advisory)
        assert st.result["final_response"] == "Judge answer."
        assert [model for model, _, _ in routes] == ["a", "b", "judge"]
        rows = db.get_messages_as_conversation(key, include_row_ids=True)
        assert [(r["role"], r["content"]) for r in rows] == [
            ("user", "Question?"), ("assistant", "Judge answer.")]
        assert agent._session_messages == session["history"] == rows
    finally:
        lease.release()
        db.close()


def test_debate_worker_cancelled_after_first_side_has_no_assistant_row(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    key, sid = "debate-cancel-stored", "debate-cancel-runtime"
    db.create_session(key, source="desktop")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    lease, refusal = try_acquire_active_session(
        session_id=key, surface="desktop", config={}, metadata={"live_session_id": sid},
        track_liveness=True)
    assert lease is not None, refusal
    agent = SimpleNamespace(session_id=key, _session_messages=[])
    session = {"session_key": key, "source": "desktop", "history": [], "history_version": 0,
               "history_lock": threading.RLock(), "running": True,
               "agent": agent, "active_session_lease": lease}
    options = DebateOptions.model_validate({
        "side_a": {"provider": "openrouter", "model": "a"},
        "side_b": {"provider": "openrouter", "model": "b"},
        "judge": {"provider": "openrouter", "model": "judge"}, "rounds": 1,
    })
    calls = []
    def advisory(route, prompt, max_tokens):
        calls.append(route.model)
        session["_turn_cancel_requested"] = True
        return "private side text"
    try:
        assert server._ensure_active_session_slot(sid, session) is None
        server._persist_submit_user_row(session, "Question?", None)
        st = SimpleNamespace(agent=agent, history_version=0, result=None)
        from tui_gateway.debate_choreography import DebateError
        with pytest.raises(DebateError, match="cancelled"):
            server._invoke_debate(sid, session, st, "Question?", "Question?", options,
                                  model_call=advisory)
        assert calls == ["a"]
        assert st.result is None
        assert [(r["role"], r["content"]) for r in db.get_messages_as_conversation(key)] == [
            ("user", "Question?")]
    finally:
        lease.release()
        db.close()


def test_isolated_debate_is_refused_before_any_mutation(monkeypatch, tmp_path):
    """Agent modes run on the session's own agent: under turn isolation they are refused, never
    downgraded to a Normal turn and never written."""
    db = SessionDB(db_path=tmp_path / "state.db")
    key, sid = "debate-vertical-stored", "debate-vertical-runtime"
    db.create_session(key, source="desktop")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **kw: True)
    monkeypatch.setattr(server, "_submit_prompt_to_compute_host", lambda *a, **kw: pytest.fail("dispatched"))
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: pytest.fail("inline fallback ran"))
    session = {
        "session_key": key, "_sid": sid, "source": "desktop", "history": [],
        "history_lock": threading.RLock(), "history_version": 0, "running": False,
        "agent": SimpleNamespace(session_id=key, _session_messages=[]),
    }
    with server._sessions_lock:
        server._sessions[sid] = session
    try:
        result = server.handle_request({"id": 1, "method": "prompt.submit", "params": {
            "session_id": sid, "text": "What is safe?",
            "execution_mode": "debate", "mode_options": {
                "side_a": {"provider": "openai", "model": "test-a"},
                "side_b": {"provider": "openai", "model": "test-b"},
                "judge": {"provider": "openai", "model": "test-judge"},
                "rounds": 1,
            },
        }})
        assert result["error"]["code"] == 4126
        assert "turn_isolation" in result["error"]["message"]
        assert session["running"] is False
        assert db.get_messages_as_conversation(key) == []
        assert "debate" in server.handle_request(
            {"id": 2, "method": "gateway.capabilities", "params": {}})["result"]["execution_modes"]
    finally:
        with server._sessions_lock:
            server._sessions.pop(sid, None)
        db.close()
