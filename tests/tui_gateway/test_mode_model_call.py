"""Advisory calls must stay on their explicitly selected, profile-owned route."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from tui_gateway.prompt_execution_modes import Route
from tui_gateway import mode_model_call
from tui_gateway.mode_model_call import ModeModelCallError, call_model


def _answer(model="vendor/test", content="advice", *, tools=None, finish="stop"):
    message = SimpleNamespace(content=content, tool_calls=tools)
    return SimpleNamespace(model=model, choices=[SimpleNamespace(message=message, finish_reason=finish)])


def test_resolves_real_profile_config_and_secrets_a_b_a(tmp_path, monkeypatch):
    from agent.secret_scope import (build_profile_secret_scope, reset_secret_scope,
                                    set_secret_scope, set_multiplex_active)
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    homes = [tmp_path / name for name in ("a", "b")]
    for home, key in zip(homes, ("a-secret", "b-secret")):
        home.mkdir()
        (home / "config.yaml").write_text("model:\n  provider: openrouter\n")
        (home / ".env").write_text(f"OPENROUTER_API_KEY={key}\n")
    set_multiplex_active(True)
    seen = []
    def transport(**kwargs):
        seen.append(kwargs)
        return _answer()
    try:
        for home in (homes[0], homes[1], homes[0]):
            home_token = set_hermes_home_override(str(home))
            secret_token = set_secret_scope(build_profile_secret_scope(home))
            try:
                assert call_model(Route(provider="openrouter", model="vendor/test"), "question", 64,
                                  transport=transport) == "advice"
            finally:
                reset_secret_scope(secret_token)
                reset_hermes_home_override(home_token)
    finally:
        set_multiplex_active(False)
    assert [x["api_key"] for x in seen] == ["a-secret", "b-secret", "a-secret"]
    assert all(x["provider"] == "openrouter" and x["model"] == "vendor/test" for x in seen)
    assert all(x["timeout"] > 0 and x["max_tokens"] == 64 and x["tools"] is None for x in seen)


def test_unresolved_provider_never_dispatches(monkeypatch):
    from hermes_cli import runtime_provider
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", lambda **kw: {
        "provider": "openrouter", "model": kw["target_model"], "api_key": "parent-key",
        "base_url": "https://openrouter.ai/api/v1", "api_mode": "chat_completions",
    })
    calls = []
    with pytest.raises(ModeModelCallError):
        call_model(Route(provider="anthropic", model="claude-test"), "question", 32,
                   transport=lambda **kw: calls.append(kw))
    assert calls == []


def test_auxiliary_transport_uses_selected_client_without_tools_or_fallback(monkeypatch):
    from agent import auxiliary_client
    from hermes_cli import runtime_provider
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", lambda **kw: {
        "provider": "openrouter", "model": kw["target_model"], "api_key": "route-key",
        "base_url": "https://openrouter.ai/api/v1", "api_mode": "chat_completions",
    })
    requests = []
    class Completion:
        def create(self, **kwargs):
            requests.append(kwargs)
            return _answer()
    client = SimpleNamespace(chat=SimpleNamespace(completions=Completion()))
    resolved = []
    def resolve(*args, **kwargs):
        resolved.append((args, kwargs))
        return client, "vendor/test"
    monkeypatch.setattr(auxiliary_client, "resolve_provider_client", resolve)
    assert call_model(Route(provider="openrouter", model="vendor/test"), "question", 32) == "advice"
    assert resolved[0][1]["explicit_api_key"] == "route-key"
    assert requests == [{
        "model": "vendor/test", "messages": [
            {"role": "system", "content": mode_model_call.ADVISORY_SYSTEM_PROMPT},
            {"role": "user", "content": "question"},
        ], "max_tokens": 32, "timeout": 30.0, "tools": None,
    }]


def test_missing_credentials_and_transport_exception_fail_without_leaking_secret(monkeypatch):
    from hermes_cli import runtime_provider
    runtime = {"provider": "openrouter", "model": "vendor/test", "api_key": "",
               "base_url": "https://openrouter.ai/api/v1", "api_mode": "chat_completions"}
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", lambda **kw: runtime)
    calls = []
    with pytest.raises(ModeModelCallError):
        call_model(Route(provider="openrouter", model="vendor/test"), "question", 32,
                   transport=lambda **kw: calls.append(kw))
    assert not calls
    runtime["api_key"] = "sensitive-key"
    def fail(**kwargs):
        raise RuntimeError("sensitive-key leaked by SDK")
    with pytest.raises(ModeModelCallError) as error:
        call_model(Route(provider="openrouter", model="vendor/test"), "question", 32,
                   transport=fail)
    assert "sensitive-key" not in str(error.value)
    assert error.value.__cause__ is None


def test_reasoning_effort_is_refused_not_silently_ignored(monkeypatch):
    from hermes_cli import runtime_provider
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", lambda **kw: {
        "provider": "openrouter", "model": kw["target_model"], "api_key": "route-key",
        "base_url": "https://openrouter.ai/api/v1", "api_mode": "chat_completions",
    })
    calls = []
    with pytest.raises(ModeModelCallError):
        call_model(Route(provider="openrouter", model="vendor/test", effort="high"),
                   "question", 32, transport=lambda **kw: calls.append(kw))
    assert calls == []


def test_bedrock_converse_without_model_id_uses_selected_route(monkeypatch):
    from agent.bedrock_adapter import normalize_converse_response
    from hermes_cli import runtime_provider
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", lambda **kw: {
        "provider": "bedrock", "model": kw["target_model"], "api_key": "route-key",
        "base_url": "https://bedrock-runtime.us-east-1.amazonaws.com", "api_mode": "bedrock_converse",
    })
    response = normalize_converse_response({
        "output": {"message": {"content": [{"text": "answer"}]}}, "stopReason": "end_turn",
    })
    assert call_model(Route(provider="bedrock", model="vendor/test"), "question", 32,
                      transport=lambda **kw: response) == "answer"


@pytest.mark.parametrize("response", [
    _answer(model="wrong"), _answer(content=""), _answer(content="x" * 8001),
    _answer(tools=[{"name": "terminal"}], finish="tool_calls"), _answer(finish="length"),
])
def test_invalid_or_misrouted_response_is_refused(monkeypatch, response):
    from hermes_cli import runtime_provider
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", lambda **kw: {
        "provider": "openrouter", "model": kw["target_model"], "api_key": "route-key",
        "base_url": "https://openrouter.ai/api/v1", "api_mode": "chat_completions",
    })
    with pytest.raises(ModeModelCallError):
        call_model(Route(provider="openrouter", model="vendor/test"), "question", 32,
                   transport=lambda **kw: response)
