"""A pool-pinned Anthropic credential must survive the per-request token refresh.

``_try_refresh_anthropic_client_credentials`` runs before every Anthropic request and re-resolves
the *ambient* token (env, ``~/.claude`` credentials, the pool's default row). When a session is
pinned to a different pool account, that ambient token belongs to ANOTHER account: adopting it
silently bills every request of the "selected" account to the ambient one, while the account menu
keeps showing the selection as ACTIVE. The refresh may only rotate the credential the session is
actually pinned to.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.client_lifecycle import ClientLifecycleMixin


def _agent(*, pinned_id=None, key="pinned-token"):
    agent = SimpleNamespace(
        api_mode="anthropic_messages", provider="anthropic", model="claude-sonnet-5",
        _anthropic_api_key=key, _anthropic_base_url="https://api.anthropic.com",
        _anthropic_client=MagicMock(), _is_anthropic_oauth=True,
        _credential_pool_entry_id=pinned_id,
        _build_direct_anthropic_client=MagicMock(return_value=MagicMock()),
        _anthropic_oauth_flag=lambda token: True,
    )
    agent._try_refresh_anthropic_client_credentials = (
        ClientLifecycleMixin._try_refresh_anthropic_client_credentials.__get__(agent))
    return agent


def test_pinned_pool_account_ignores_the_ambient_token():
    agent = _agent(pinned_id="hadota")
    pool = SimpleNamespace(entries=lambda: [
        SimpleNamespace(id="eid", runtime_api_key="ambient-token"),
        SimpleNamespace(id="hadota", runtime_api_key="pinned-token"),
    ])
    with patch("agent.anthropic_credentials.resolve_anthropic_token", return_value="ambient-token"), \
         patch("agent.credential_pool.load_pool", return_value=pool):
        assert agent._try_refresh_anthropic_client_credentials() is False
    assert agent._anthropic_api_key == "pinned-token"
    agent._build_direct_anthropic_client.assert_not_called()


def test_pinned_pool_account_adopts_its_own_rotated_token():
    agent = _agent(pinned_id="hadota", key="old-hadota-token")
    pool = SimpleNamespace(entries=lambda: [
        SimpleNamespace(id="eid", runtime_api_key="ambient-token"),
        SimpleNamespace(id="hadota", runtime_api_key="new-hadota-token"),
    ])
    with patch("agent.anthropic_credentials.resolve_anthropic_token", return_value="ambient-token"), \
         patch("agent.credential_pool.load_pool", return_value=pool):
        assert agent._try_refresh_anthropic_client_credentials() is True
    assert agent._anthropic_api_key == "new-hadota-token"


def test_unpinned_agent_keeps_ambient_refresh():
    agent = _agent(pinned_id=None, key="stale")
    with patch("agent.anthropic_credentials.resolve_anthropic_token", return_value="fresh"):
        assert agent._try_refresh_anthropic_client_credentials() is True
    assert agent._anthropic_api_key == "fresh"
