"""/delegate in messaging chats: the switch is per chat, reads its lanes from config, and a
Delegate turn runs the orchestrator gate while the user's own words stay the transcript."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource

LANES = {"workers": [{"lane": "code", "provider": "anthropic", "model": "claude-sonnet-5", "effort": "medium"}]}


def _event(text, chat_id="c1"):
    return MessageEvent(text=text, source=SessionSource(platform=Platform.DISCORD, user_id="u1", chat_id=chat_id,
                                                         user_name="ahmed"))


def _runner(config):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner._load_delegate_config = lambda: config
    runner._normalize_source_for_session_key = lambda source: source
    return runner


def test_switch_is_per_chat_and_refuses_without_lanes():
    runner = _runner({"delegate_mode": LANES})
    on = asyncio.run(runner._handle_delegate_command(_event("/delegate on")))
    assert "ON" in on and "claude-sonnet-5" in on
    key_c1 = runner._session_key_for_source(_event("x").source)
    key_c2 = runner._session_key_for_source(_event("x", chat_id="c2").source)
    assert runner._delegate_enabled(key_c1) and not runner._delegate_enabled(key_c2)

    asyncio.run(runner._handle_delegate_command(_event("/delegate off")))
    assert not runner._delegate_enabled(key_c1)

    unconfigured = _runner({})
    refusal = asyncio.run(unconfigured._handle_delegate_command(_event("/delegate on")))
    assert "delegate_mode" in refusal
    assert not unconfigured._delegate_enabled(key_c1)


def test_delegate_turn_arms_the_gate_and_reports_interruption(monkeypatch):
    from agent import delegate_orchestrator as orch

    runner = _runner({"delegate_mode": LANES})
    ctx = SimpleNamespace(session_key="k1", session_id="sid1", source=_event("x").source,
                          _run_still_current=lambda: True)
    runner._session_state("k1").conversation.delegate_mode = True
    notices = []
    runner._deliver_platform_notice = lambda source, line: notices.append(line)
    turn_runner = TurnRunner.__new__(TurnRunner)
    turn_runner._runner, turn_runner._ctx = runner, ctx
    turn_runner._schedule = lambda coro, _msg: None

    agent = SimpleNamespace(reasoning_config=None)
    with turn_runner._delegate_turn(agent) as note:
        assert note and "`code`" in note
        assert orch.blocked_message("write_file", "sid1") is not None  # the orchestrator may not write
        assert orch.routing_for_tasks(agent, [{"goal": "x"}])[0]["model"] == "claude-sonnet-5"
        orch.before_dispatch(agent)
        turn_runner._mark_delegate_result({"interrupted": True})
    assert orch.blocked_message("write_file", "sid1") is None  # gate lifted after the turn
    assert any("interrupted" in line for line in notices)

    runner._session_state("k1").conversation.delegate_mode = False
    with turn_runner._delegate_turn(agent) as note:
        assert note is None
