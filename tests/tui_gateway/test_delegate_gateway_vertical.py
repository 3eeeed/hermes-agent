"""Real Delegate admission must use child lifecycle and one parent answer."""
import io
import json
import threading
from types import SimpleNamespace

import pytest

from hermes_cli.active_sessions import try_acquire_active_session
from hermes_state import SessionDB
from tui_gateway import server


@pytest.mark.parametrize("isolated", [False, True])
def test_delegate_submit_commits_one_orchestrated_answer(monkeypatch, tmp_path, isolated):
    import tui_gateway.delegate_readonly_lifecycle as lifecycle
    db = SessionDB(db_path=tmp_path / "state.db")
    key, sid = "delegate-stored", "delegate-runtime"
    db.create_session(key, source="desktop")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    lease, refusal = try_acquire_active_session(
        session_id=key, surface="desktop", config={}, metadata={"live_session_id": sid},
        track_liveness=True)
    assert lease is not None, refusal
    agent = SimpleNamespace(session_id=key, _session_messages=[],
                            interim_assistant_callback=None,
                            run_conversation=lambda *a, **k: pytest.fail("normal agent ran"),
                            clear_interrupt=lambda: None)
    session = {"session_key": key, "source": "desktop", "history": [],
               "history_lock": threading.RLock(), "history_version": 0,
               "running": False, "agent": agent, "agent_ready": threading.Event(),
               "active_session_lease": lease, "attached_images": [], "cols": 80}
    session["agent_ready"].set()
    called, events = [], []
    completed = threading.Event()
    def capture(*args):
        events.append(args)
        if args[0] == "message.complete":
            completed.set()
    monkeypatch.setattr(server, "_emit", capture)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *a: None)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **kw: isolated)
    host = None
    child_agent = None
    if isolated:
        from tui_gateway.compute_host import ComputeHost
        out = io.StringIO()
        host = ComputeHost(stdout=out, heartbeat_secs=0)
        child_agent = SimpleNamespace(_session_messages=[])
        child = {"session_key": key, "history_lock": threading.RLock(), "running": False,
                 "agent": child_agent, "history": [], "history_version": 0,
                 "_turn_cancel_requested": False}
        monkeypatch.setattr(host, "_ensure_server_session", lambda *a: child)
        monkeypatch.setattr(server, "_install_borrowed_lease", lambda *a: None)
        class Supervisor:
            def submit_turn(self, frame, on_complete):
                host._run_real_turn(frame)
                terminals = [json.loads(line) for line in out.getvalue().splitlines()
                             if json.loads(line).get("type") in {"turn.end", "turn.error"}]
                assert len(terminals) == 1, terminals
                on_complete(terminals[0])
                on_complete(terminals[0])
        monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: Supervisor())
    monkeypatch.setattr(server, "_prepare_turn_input", lambda sid, sess, st, text, images:
                        (text, text, 80, None))
    monkeypatch.setattr(server, "_finish_turn", lambda *a: None)
    monkeypatch.setattr(server, "_goal_followup_after_turn", lambda *a: None)
    monkeypatch.setattr(server, "_after_complete_turn", lambda *a: None)
    monkeypatch.setattr(server, "_publish_session_control_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_emit_settled_session_info", lambda *a: None)
    def real_children(parent, prompt, options, *, profile_home, cancelled, context=""):
        assert not cancelled()
        called.append((parent, prompt, options, profile_home))
        return "Orchestrated answer.", [{"status": "completed", "summary": "research"}]
    monkeypatch.setattr(lifecycle, "run_readonly_delegate_answer", real_children)
    options = {"orchestrator": {"provider": "openrouter", "model": "judge"},
               "workers": [{"lane": "research", "provider": "openrouter", "model": "worker"}],
               "max_iterations": 2, "max_concurrency": 1, "permissions": "read-only"}
    with server._sessions_lock:
        server._sessions[sid] = session
    try:
        response = server.handle_request({"id": 1, "method": "prompt.submit", "params": {
            "session_id": sid, "text": "Question?", "client_send_id": "delegate-send",
            "execution_mode": "delegate", "mode_options": options}})
        assert response["result"]["status"] == "streaming"
        assert completed.wait(5)
        assert len(called) == 1 and called[0][0] is (child_agent if isolated else agent)
        assert called[0][1] == "Question?"
        assert [(m["role"], m["content"]) for m in db.get_messages_as_conversation(key)] == [
            ("user", "Question?"), ("assistant", "Orchestrated answer.")]
        assert [e[2]["text"] for e in events if e[0] == "message.complete"] == ["Orchestrated answer."]
    finally:
        with server._sessions_lock:
            server._sessions.pop(sid, None)
        lease.release()
        if host is not None:
            host.close()
        db.close()
