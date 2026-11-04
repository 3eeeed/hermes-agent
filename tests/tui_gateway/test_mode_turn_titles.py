"""Mode turns are titled from the user's own words: Delegate passes the typed prompt, Debate titles at all."""
import contextlib
import inspect
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_cli.active_sessions import try_acquire_active_session
from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.prompt_execution_modes import DebateOptions

DELEGATE_NOTE = "[Delegate mode is on for this message. You lead; workers implement.]\nLanes:\n- `x`: p/m"


def test_invoke_agent_passes_typed_prompt_as_title_text(monkeypatch):
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_load_interim_assistant_messages", lambda: False)
    monkeypatch.setattr(server, "_start_usage_ticker", lambda _sid, _agent: (
        SimpleNamespace(set=lambda: None), SimpleNamespace(join=lambda: None)))
    seen = {}

    def run_conversation(user_message, conversation_history=None, stream_callback=None,
                         persist_user_message=None, title_user_message=None):
        seen.update(message=user_message, title=title_user_message)
        return {"final_response": "done"}

    agent = SimpleNamespace(run_conversation=run_conversation)
    session = {"pending_title": None, "session_key": "k", "history_lock": contextlib.nullcontext(), "agent": agent}
    st = server._TurnRun(agent=agent, one_turn_restore=None, terminal_callback=None, receipt_committed=True)
    prompt = "Refactor the billing exporter"
    server._invoke_agent("sid", session, st, prompt, f"{DELEGATE_NOTE}\n\n{prompt}", None, [], None, None)
    assert seen["message"].startswith("[Delegate mode is on")
    assert seen["title"] == prompt


def test_invoke_agent_omits_title_text_for_old_run_conversation(monkeypatch):
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_load_interim_assistant_messages", lambda: False)
    monkeypatch.setattr(server, "_start_usage_ticker", lambda _sid, _agent: (
        SimpleNamespace(set=lambda: None), SimpleNamespace(join=lambda: None)))
    seen = {}

    def run_conversation(user_message, conversation_history=None, stream_callback=None, persist_user_message=None):
        seen["ok"] = True
        return {"final_response": "done"}

    agent = SimpleNamespace(run_conversation=run_conversation)
    assert "title_user_message" not in inspect.signature(run_conversation).parameters
    session = {"pending_title": None, "session_key": "k", "history_lock": contextlib.nullcontext(), "agent": agent}
    st = server._TurnRun(agent=agent, one_turn_restore=None, terminal_callback=None, receipt_committed=True)
    server._invoke_agent("sid", session, st, "hi", "hi", None, [], None, None)
    assert seen == {"ok": True}


def _llm(title):
    def stub(**kwargs):
        resp = MagicMock()
        resp.choices[0].message.content = f'{{"title": "{title}"}}'
        resp.choices[0].message.reasoning = None
        resp.choices[0].message.reasoning_content = None
        return resp
    return stub


def test_debate_turn_is_auto_titled_and_renamed_live(monkeypatch, tmp_path):
    from agent.title_generator import wait_for_title_upgrades
    db = SessionDB(db_path=tmp_path / "state.db")
    key, sid = "debate-title-stored", "debate-title-runtime"
    db.create_session(key, source="desktop")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    events = []
    monkeypatch.setattr(server, "_emit", lambda *a: events.append(a))
    lease, refusal = try_acquire_active_session(
        session_id=key, surface="desktop", config={}, metadata={"live_session_id": sid},
        track_liveness=True)
    assert lease is not None, refusal
    agent = SimpleNamespace(session_id=key, _session_messages=[], _session_db=db, _session_db_created=True,
                            platform="desktop", model="m", provider="openrouter", base_url="", api_key="",
                            api_mode="")
    session = {"session_key": key, "source": "desktop", "history": [], "history_version": 0,
               "history_lock": threading.RLock(), "running": True,
               "agent": agent, "active_session_lease": lease}
    options = DebateOptions.model_validate({
        "side_a": {"provider": "openrouter", "model": "a"},
        "side_b": {"provider": "openrouter", "model": "b"},
        "judge": {"provider": "openrouter", "model": "judge"}, "rounds": 1,
    })

    def advisory(route, prompt, max_tokens):
        return "Judge answer." if route.model == "judge" else route.model + " argument"
    try:
        assert server._ensure_active_session_slot(sid, session) is None
        question = "Should we shard the Postgres cluster?"
        server._persist_submit_user_row(session, question, None)
        st = SimpleNamespace(agent=agent, history_version=0, result=None)
        with patch("agent.title_generator.call_llm", side_effect=_llm("Postgres cluster sharding decision")), \
                patch("agent.title_generator._title_config", return_value={}):
            server._invoke_debate(sid, session, st, question, question, options, model_call=advisory)
            wait_for_title_upgrades(10)
        assert st.result["final_response"] == "Judge answer."
        assert db.get_session_title(key) == "Postgres cluster sharding decision"
        assert db.get_session_title_source(key) == "llm"
        titles = [e[2]["title"] for e in events if e[0] == "session.title"]
        assert titles[-1] == "Postgres cluster sharding decision"
        assert all(e[2]["session_id"] == key for e in events if e[0] == "session.title")
    finally:
        lease.release()
        db.close()
