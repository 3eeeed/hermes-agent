"""Debate/Delegate advisory calls must see the conversation they answer.

Regression: a live Delegate turn answered the follow-up "I sent it from the
MacBook" as a context-free question and claimed to be a read-only advisor that
cannot act, because no model in the choreography received session history.
"""
import threading
from types import SimpleNamespace

from tui_gateway.debate_choreography import run_debate
from tui_gateway.mode_context import conversation_excerpt
from tui_gateway.prompt_execution_modes import DebateOptions

HISTORY = [
    {"role": "user", "content": "Open the test window on the MacBook."},
    {"role": "assistant", "content": "Send me a message from the Agent Modes Test window.", "tool_calls": None},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "t", "function": {"name": "terminal"}}]},
    {"role": "tool", "content": "SECRET_TOOL_OUTPUT"},
    {"role": "user", "content": "[System note: interrupted]", "display_kind": "auto_continue"},
    {"role": "user", "content": "I sent it from the MacBook"},
]


def _debate_options():
    return DebateOptions.model_validate({
        "side_a": {"provider": "p", "model": "a"}, "side_b": {"provider": "p", "model": "b"},
        "judge": {"provider": "p", "model": "judge"}, "rounds": 1}, strict=True)


def test_excerpt_keeps_visible_turns_drops_tools_notices_and_current_prompt():
    excerpt = conversation_excerpt(HISTORY, "I sent it from the MacBook")
    assert "Agent Modes Test window" in excerpt and "Open the test window" in excerpt
    assert "SECRET_TOOL_OUTPUT" not in excerpt and "System note" not in excerpt
    assert "I sent it from the MacBook" not in excerpt
    assert excerpt.index("Open the test window") < excerpt.index("Agent Modes Test")


def test_excerpt_is_bounded_and_prefers_newest_turns():
    history = [{"role": "user", "content": f"old-{i} " + "x" * 900} for i in range(20)]
    history.append({"role": "assistant", "content": "newest answer"})
    excerpt = conversation_excerpt(history, "next")
    assert len(excerpt) <= 3_000 and excerpt.endswith("newest answer")
    assert "old-0 " not in excerpt


def test_every_debate_call_receives_the_conversation():
    prompts = []
    def model_call(route, prompt, max_tokens):
        prompts.append(prompt)
        return "judge answer" if route.model == "judge" else "side"
    context = conversation_excerpt(HISTORY, "I sent it from the MacBook")
    run_debate("I sent it from the MacBook", _debate_options(), model_call,
               cancelled=lambda: False, deadline_exceeded=lambda: False, context=context)
    assert len(prompts) == 3
    assert all("Agent Modes Test window" in p and "I sent it from the MacBook" in p for p in prompts)


def test_inline_debate_passes_session_history(monkeypatch, tmp_path):
    from hermes_state import SessionDB
    from hermes_cli.active_sessions import try_acquire_active_session
    from tui_gateway import server
    db = SessionDB(db_path=tmp_path / "state.db")
    key, sid = "ctx-debate-stored", "ctx-debate-runtime"
    db.create_session(key, source="desktop")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    lease, refusal = try_acquire_active_session(
        session_id=key, surface="desktop", config={}, metadata={"live_session_id": sid},
        track_liveness=True)
    assert lease is not None, refusal
    agent = SimpleNamespace(session_id=key, _session_messages=[],
                            run_conversation=lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    session = {"session_key": key, "source": "desktop", "history": list(HISTORY[:2]),
               "history_version": 0, "history_lock": threading.RLock(), "running": True,
               "agent": agent, "active_session_lease": lease}
    prompts = []
    def advisory(route, prompt, max_tokens):
        prompts.append(prompt)
        return "Judge answer." if route.model == "judge" else "side"
    try:
        assert server._reserve_session_turn(sid, session) is None
        server._persist_submit_user_row(session, "I sent it from the MacBook", None)
        st = SimpleNamespace(agent=agent, history=list(HISTORY[:2]), history_version=0, result=None)
        server._invoke_debate(sid, session, st, "I sent it from the MacBook",
                              "I sent it from the MacBook", _debate_options(), model_call=advisory)
        assert prompts and all("Agent Modes Test window" in p for p in prompts)
    finally:
        server._finish_session_turn(sid, session)
        lease.release()
        db.close()


def test_delegate_children_and_synthesis_receive_the_conversation(monkeypatch):
    import tools.delegate_tool_results as results
    import hermes_cli.runtime_provider as providers
    from tui_gateway.delegate_readonly_lifecycle import run_readonly_delegate_answer
    monkeypatch.setattr(providers, "resolve_runtime_provider", lambda *, requested, target_model: {
        "provider": requested, "model": target_model, "api_key": "k",
        "base_url": "https://provider.example/v1", "api_mode": "chat_completions"})
    built = []
    def build(**kwargs):
        built.append(kwargs)
        return SimpleNamespace(valid_tool_names={"web_search"}, close=lambda: None)
    monkeypatch.setattr(results, "_build_child_preserving_parent_tools", build)
    monkeypatch.setattr(results, "_run_child_lifecycle", lambda i, goal, child, parent:
                        {"task_index": i, "status": "completed", "summary": "finding"})
    seen = []
    options = {"orchestrator": {"provider": "judge", "model": "m"},
               "workers": [{"lane": "alpha", "provider": "a", "model": "m"}],
               "max_iterations": 2, "max_concurrency": 1, "permissions": "read-only"}
    context = conversation_excerpt(HISTORY, "I sent it from the MacBook")
    run_readonly_delegate_answer(SimpleNamespace(), "I sent it from the MacBook", options,
                                 profile_home=None, context=context,
                                 synthesize=lambda r, p, n: seen.append(p) or "answer")
    assert "Agent Modes Test window" in built[0]["context"]
    assert "Agent Modes Test window" in seen[0]
