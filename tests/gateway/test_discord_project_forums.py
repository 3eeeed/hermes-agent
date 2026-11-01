"""Local Discord project-forum patches: channel cwd binding, thread->session binding, status card
render, forum state tags, Discord auto-approve. Behaviour contracts, real imports, temp HERMES_HOME."""

import asyncio
import json
import types

import pytest

from gateway.config import Platform
from gateway.session import SessionSource


def _runner_with_cwds(mapping):
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    adapter = types.SimpleNamespace(config=types.SimpleNamespace(extra={"channel_cwds": mapping}),
                                    supports_async_delivery=True)
    runner.adapters = {Platform.DISCORD: adapter}
    return runner


def test_forum_post_inherits_forum_folder_and_pins_agent_cwd(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    runner = _runner_with_cwds({"111": str(proj)})
    post = SessionSource(platform=Platform.DISCORD, chat_id="999", chat_type="thread", thread_id="999",
                         parent_chat_id="111", user_id="u")
    other = SessionSource(platform=Platform.DISCORD, chat_id="5", chat_type="group", user_id="u")
    assert runner._channel_cwd_for(post) == str(proj)
    assert runner._channel_cwd_for(other) is None

    tokens = runner._set_session_env(types.SimpleNamespace(source=post, session_key="k1", session_id="s1"))
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        assert resolve_agent_cwd() == proj
    finally:
        runner._clear_session_env(tokens)


def test_channel_cwd_seeds_terminal_and_persists_session_row(tmp_path):
    from gateway.channel_cwd import bind_session_to_channel_cwd
    from hermes_state import SessionDB
    from tools.terminal_tool import _resolve_command_cwd
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    db = SessionDB()
    db.create_session("s1", "discord")
    bind_session_to_channel_cwd(str(proj), "key-a", "s1", db)
    row = db.get_session("s1")
    assert row["cwd"] == str(proj) and row["git_repo_root"] == str(proj)
    assert _resolve_command_cwd(workdir=None, default_cwd="/", session_key="key-a") == str(proj)


def test_thread_binding_switches_only_an_empty_thread_session(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    from gateway import thread_session_bindings as tsb
    from hermes_state import SessionDB

    db = SessionDB()
    db.create_session("old", "desktop")
    db.append_message("old", "user", "hello from desktop")
    db.create_session("fresh", "discord")
    (get_hermes_home() / "thread_session_bindings.json").write_text(json.dumps({"discord": {"777": "old"}}))

    switched = []

    class Store:
        async def switch_session(self, key, target):
            switched.append((key, target))
            return types.SimpleNamespace(session_id=target)

    runner = types.SimpleNamespace(_session_db=db, async_session_store=Store(), _evict_cached_agent=lambda k: None)
    src = SessionSource(platform=Platform.DISCORD, chat_id="777", chat_type="thread", thread_id="777", user_id="u")
    entry = asyncio.run(tsb.apply_thread_session_binding(runner, src, types.SimpleNamespace(session_id="fresh"), "k"))
    assert entry.session_id == "old" and switched == [("k", "old")]

    # after /new the thread owns a conversation of its own: the binding must not pull it back
    db.append_message("fresh", "user", "new work")
    switched.clear()
    entry = asyncio.run(tsb.apply_thread_session_binding(runner, src, types.SimpleNamespace(session_id="fresh"), "k"))
    assert entry.session_id == "fresh" and not switched


def test_status_card_reflects_live_agent_state():
    from gateway.discord_status_card import render_card
    agent = types.SimpleNamespace(model="vendor/model-x", provider="", reasoning_config={"effort": "high"},
                                  service_tier=None, _credential_pool=None)
    card = render_card(agent, {"last_prompt_tokens": 50_000, "context_length": 200_000}, "/srv/p")
    assert "model-x" in card and "vendor/" not in card
    assert "high" in card and "25%" in card and "/srv/p" in card


@pytest.mark.parametrize("platform, expected", [(Platform.DISCORD, True), (Platform.TELEGRAM, False)])
def test_auto_approve_is_discord_scoped_and_config_gated(platform, expected, monkeypatch):
    from gateway.run import GatewayRunner
    import gateway.run as run_mod
    from tools.approval import is_session_yolo_enabled, disable_session_yolo
    monkeypatch.setattr(run_mod, "_load_gateway_config", lambda: {"discord": {"auto_approve": True}})
    runner = object.__new__(GatewayRunner)
    key = f"k-{platform.value}"
    disable_session_yolo(key)
    runner._hmwa_discord_auto_approve(SessionSource(platform=platform, chat_id="1", chat_type="thread",
                                                    thread_id="1", user_id="u"), key)
    assert is_session_yolo_enabled(key) is expected

    monkeypatch.setattr(run_mod, "_load_gateway_config", lambda: {"discord": {}})
    disable_session_yolo("k-off")
    runner._hmwa_discord_auto_approve(SessionSource(platform=Platform.DISCORD, chat_id="1", chat_type="thread",
                                                    thread_id="1", user_id="u"), "k-off")
    assert not is_session_yolo_enabled("k-off")


def test_channel_cwds_yaml_reaches_adapter_extra_and_keys_are_registered(tmp_path, monkeypatch):
    from pathlib import Path
    from gateway.config import load_gateway_config
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "config.yaml").write_text(
        "discord:\n  channel_cwds:\n    111: /srv/proj\n", encoding="utf-8")
    extra = load_gateway_config().platforms[Platform.DISCORD].extra
    assert extra["channel_cwds"] == {"111": "/srv/proj"}
    for key in ("channel_cwds", "status_card", "auto_approve"):
        assert key in DEFAULT_CONFIG["discord"]
    assert "channel_cwds" in DEFAULT_CONFIG["slack"]
