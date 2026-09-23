"""Compute-host (turn isolation) dispatch from ``prompt.submit``.

Split out of ``test_tui_gateway_server.py`` (file-size ratchet). The parent now writes the
submit-time user row before dispatch so the send ledger can bind it; the child adopts it.
"""
import threading
import types

from tui_gateway import server


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        **extra,
    }


def test_prompt_submit_dispatches_to_compute_host_when_turn_isolation_enabled(monkeypatch):
    class FakeSupervisor:
        def __init__(self):
            self.frames = []
            self.callback = None

        def submit_turn(self, frame, *, on_complete=None):
            self.frames.append(frame)
            self.callback = on_complete
            return frame["request_id"]

    fake_supervisor = FakeSupervisor()
    seed_history = [{"role": "user", "content": "previous"}]
    server._sessions["iso-sid"] = _session(history=list(seed_history))
    server._sessions["iso-sid"]["agent"] = None
    server._sessions["iso-sid"]["agent_ready"] = threading.Event()
    parent_writes = {"ensure_session": 0, "persist_seed": 0, "user_row": 0}
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {"dashboard": {"turn_isolation": True}},
    )
    monkeypatch.setattr(
        server,
        "_ensure_session_db_row",
        lambda _session: parent_writes.__setitem__(
            "ensure_session", parent_writes["ensure_session"] + 1
        ),
    )
    monkeypatch.setattr(
        server,
        "_persist_branch_seed",
        lambda _session: parent_writes.__setitem__(
            "persist_seed", parent_writes["persist_seed"] + 1
        ),
    )
    monkeypatch.setattr(server, "_persist_submit_user_row", lambda session, text, *_a, **_k: (
        parent_writes.__setitem__("user_row", parent_writes["user_row"] + 1),
        session.__setitem__("_submit_user_row", {"role": "user", "content": text, "_row_id": 77, "_db_persisted": True})))
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda _cfg=None: fake_supervisor)

    try:
        resp = server.handle_request(
            {
                "id": "submit",
                "method": "prompt.submit",
                "params": {"session_id": "iso-sid", "text": "hello"},
            }
        )
        assert resp["result"] == {"status": "streaming", "turn_isolation": True}
        assert fake_supervisor.frames[0]["type"] == "turn.start"
        assert fake_supervisor.frames[0]["sid"] == "iso-sid"
        assert fake_supervisor.frames[0]["text"] == "hello"
        assert fake_supervisor.frames[0]["history"] == seed_history
        assert server._sessions["iso-sid"]["history"] == seed_history
        assert parent_writes == {"ensure_session": 1, "persist_seed": 1, "user_row": 1}
        assert fake_supervisor.frames[0]["submit_user_row"]["_row_id"] == 77
        assert server._sessions["iso-sid"]["running"] is True

        fake_supervisor.callback(
            {
                "type": "turn.end",
                "sid": "iso-sid",
                "request_id": "submit",
                "history_version": 1,
            }
        )
        assert server._sessions["iso-sid"]["running"] is False
        assert server._sessions["iso-sid"]["history_version"] == 1
    finally:
        server._sessions.pop("iso-sid", None)
