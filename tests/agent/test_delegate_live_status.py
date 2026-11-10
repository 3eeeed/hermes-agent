"""A message typed while a Delegate worker runs gets an instant status reply.

The orchestrator is blocked inside ``delegate_task`` until the worker returns, so the steer
only reaches the model afterwards; the live status covers that wait.
"""
import subprocess
import threading
import types

import pytest

from agent import delegate_orchestrator as orch
from agent import delegate_status
from tui_gateway import server

OPTIONS = {"workers": [{"lane": "code", "provider": "anthropic", "model": "claude-sonnet-5"}]}


class _Agent:
    reasoning_config = None
    session_id = "parent-session"

    def steer(self, text):
        return True


@pytest.fixture
def repo(tmp_path, monkeypatch):
    subprocess.run("git init -q && echo a > a.txt && git add a.txt && "
                   "git -c user.email=t@t -c user.name=t commit -qm init", shell=True, check=True, cwd=tmp_path)
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    return tmp_path


def test_status_names_lane_model_goal_only_while_the_worker_runs(repo):
    agent = _Agent()
    with orch.orchestrator_turn(agent, "turn-s", OPTIONS):
        agent._delegate_task_id = "turn-s"
        assert delegate_status.status_reply("turn-s") is None  # planning: the model itself answers
        assert orch.before_dispatch(agent, goal="Fix the login redirect in auth.py", model="claude-sonnet-5") is None
        reply = delegate_status.status_reply("turn-s")
        assert "`code`" in reply and "claude-sonnet-5" in reply
        assert "Fix the login redirect in auth.py" in reply
        assert "1 من 3" in reply
        orch.after_dispatch(agent, "{}")
        assert delegate_status.status_reply("turn-s") is None  # review: back to the model
    assert delegate_status.status_reply("turn-s") is None


def test_status_reports_the_workers_tool_activity(repo, monkeypatch):
    import tools.delegate_tool_registry as registry
    monkeypatch.setattr(registry, "list_active_subagents", lambda: [
        {"owner_agent_session_id": "someone-else", "status": "running", "tool_count": 99, "last_tool": "x"},
        {"owner_agent_session_id": "parent-session", "status": "running", "tool_count": 4, "last_tool": "patch"},
    ])
    agent = _Agent()
    with orch.orchestrator_turn(agent, "turn-t", OPTIONS):
        orch.before_dispatch(agent, goal="g")
        reply = delegate_status.status_reply("turn-t")
    assert "4 أداة" in reply and "`patch`" in reply and "99" not in reply


@pytest.mark.parametrize("method", ["session.steer", "session.redirect"])
def test_a_mid_worker_correction_is_answered_at_once(repo, monkeypatch, method):
    agent = _Agent()
    agent.redirect = lambda text: True
    agent._supports_active_turn_redirect = True
    emitted = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: emitted.append((event, sid, payload)))
    server._sessions["sid"] = {"agent": agent, "session_key": "k", "history": [], "running": True,
                               "history_lock": threading.Lock(), "attached_images": []}
    try:
        with orch.orchestrator_turn(agent, "turn-r", OPTIONS):
            orch.before_dispatch(agent, goal="Add rate limiting", model="claude-sonnet-5")
            resp = server.handle_request({"id": "1", "method": method,
                                          "params": {"session_id": "sid", "text": "بتعمل ايه؟"}})
        assert resp["result"]["status"] in {"queued", "redirected"}, resp
        interim = [p for e, s, p in emitted if e == "message.interim" and s == "sid"]
        assert len(interim) == 1 and "Add rate limiting" in interim[0]["text"]
    finally:
        server._sessions.pop("sid", None)


def test_a_correction_outside_delegate_emits_nothing(monkeypatch):
    emitted = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: emitted.append(event))
    agent = types.SimpleNamespace(steer=lambda text: True)
    server._sessions["sid"] = {"agent": agent, "session_key": "k", "history": [], "running": True,
                               "history_lock": threading.Lock(), "attached_images": []}
    try:
        resp = server.handle_request({"id": "1", "method": "session.steer",
                                      "params": {"session_id": "sid", "text": "hi"}})
    finally:
        server._sessions.pop("sid", None)
    assert resp["result"]["status"] == "queued"
    assert "message.interim" not in emitted


def test_turn_note_asks_for_handoff_lines_and_mid_turn_replies():
    note = orch.turn_note(OPTIONS)
    assert "right before each delegate_task" in note
    assert "reply to that message first" in note
