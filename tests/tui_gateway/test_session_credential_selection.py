"""Desktop dispatch must honour the persisted account, not the pool default."""
import contextlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tui_gateway import server


@pytest.fixture
def selection(monkeypatch):
    entries = [SimpleNamespace(id="first"), SimpleNamespace(id="chosen")]
    db = Mock()
    db.get_session_model_config_value.return_value = "chosen"
    agent = SimpleNamespace(
        provider="openai-codex", session_id="stored-session",
        _credential_pool=SimpleNamespace(entries=lambda: entries),
        _swap_credential=Mock(return_value=True),
    )
    session = {"agent": agent, "session_key": "stored-session"}
    monkeypatch.setattr(server, "_session_db", lambda s: contextlib.nullcontext(db))
    return session, agent, db, entries


def test_desktop_applies_pin_and_reloads_it_each_turn(selection):
    session, agent, db, entries = selection
    server._apply_session_credential_selection(session, agent)
    agent._swap_credential.assert_called_with(entries[1])
    db.get_session_model_config_value.assert_called_with("stored-session", "openai_codex_credential_id")
    db.get_session_model_config_value.return_value = "first"
    server._apply_session_credential_selection(session, agent)
    agent._swap_credential.assert_called_with(entries[0])


@pytest.mark.parametrize("failure", ["missing", "rejected", "no-pool"])
def test_no_silent_fallback(selection, failure):
    session, agent, db, entries = selection
    if failure == "missing":
        db.get_session_model_config_value.return_value = "deleted"
    elif failure == "rejected":
        agent._swap_credential.return_value = False
    else:
        agent._credential_pool = None
    with pytest.raises(RuntimeError):
        server._apply_session_credential_selection(session, agent)


def test_rotation_persists_replacement_for_live_session(selection):
    session, agent, db, entries = selection
    server._apply_session_credential_selection(session, agent)
    agent.session_id = "compressed-session"
    agent.credential_rotation_callback(entries[0])
    db.patch_session_model_config.assert_called_once_with(
        "compressed-session", {"openai_codex_credential_id": "first"})


def test_no_pin_keeps_default_and_wires_rotation(selection):
    session, agent, db, entries = selection
    db.get_session_model_config_value.return_value = None
    server._apply_session_credential_selection(session, agent)
    agent._swap_credential.assert_not_called()
    agent.credential_rotation_callback(entries[1])
    db.patch_session_model_config.assert_called_once()


def test_invoke_stops_before_conversation_when_selection_fails(selection):
    session, agent, db, entries = selection
    agent.run_conversation = Mock()
    agent._swap_credential.return_value = False
    with pytest.raises(RuntimeError):
        server._invoke_agent("runtime-id", session, SimpleNamespace(agent=agent),
                             "hello", "hello", None, [], None, None)
    agent.run_conversation.assert_not_called()


def test_real_swap_rebuilds_transport_with_selected_bearer(selection):
    """Exercise the production swap, then inspect an SDK-built HTTP request (no network)."""
    import types
    from openai import OpenAI
    from agent.client_lifecycle import ClientLifecycleMixin

    session, agent, db, entries = selection
    agent.api_mode = "codex_responses"
    agent.model = "gpt-5-codex"
    agent.api_key = "test-first-token"
    agent.base_url = "https://chatgpt.com/backend-api/codex"
    agent._client_kwargs = {"api_key": agent.api_key, "base_url": agent.base_url}
    entries[1].runtime_api_key = "test-chosen-token"
    entries[1].runtime_base_url = agent.base_url
    agent._reapply_route_client_config = Mock()
    clients = []

    def rebuild(**kwargs):
        agent.client = OpenAI(**agent._client_kwargs)
        clients.append(agent.client)

    agent._replace_primary_openai_client = rebuild
    agent._swap_credential = types.MethodType(ClientLifecycleMixin._swap_credential, agent)
    try:
        server._apply_session_credential_selection(session, agent)
        from openai._models import FinalRequestOptions
        request = agent.client._build_request(FinalRequestOptions.construct(method="post", url="/responses"))
        assert request.headers["Authorization"] == "Bearer test-chosen-token"
        assert agent._credential_pool_entry_id == "chosen"
    finally:
        for client in clients:
            client.close()
