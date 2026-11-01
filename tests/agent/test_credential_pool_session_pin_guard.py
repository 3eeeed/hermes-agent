"""A session pinned to one pooled account must not silently rotate onto another.

The desktop account menu lets a user pin a specific Codex/Anthropic account to a
chat (``agent/credential_pool.py::POOL_PIN_KEYS``). Before this fix, once the
pinned account hit a 429/402/401, ``recover_with_credential_pool`` rotated the
SAME session onto whichever other pool entry was next in line and persisted
that as the new pin — silently spending a different account's quota for a
choice the user made on purpose. The guard refuses the swap when the rotated-to
entry does not match ``agent._session_pinned_credential_id`` (set by
``gateway/run_turn_runner.py`` from the session's stored pin at turn-wiring
time) and surfaces the failure instead, exactly like an unpinned exhausted pool.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.agent_runtime_helpers import recover_with_credential_pool
from agent.error_classifier import FailoverReason


def _agent(pinned_credential_id):
    agent = MagicMock()
    agent.provider = "anthropic"
    agent.base_url = "https://api.anthropic.com"
    agent.api_key = "key-a"
    agent._credential_pool_entry_id = "cred-a"
    agent._session_pinned_credential_id = pinned_credential_id
    pool = MagicMock()
    pool.provider = "anthropic"
    agent._credential_pool = pool
    return agent, pool


def test_pinned_session_does_not_auto_rotate_to_a_different_account():
    agent, pool = _agent(pinned_credential_id="cred-a")
    pool.entries.return_value = []
    pool.current.return_value = SimpleNamespace(id="cred-a", runtime_api_key="key-a")
    other_entry = SimpleNamespace(id="cred-b", runtime_api_key="key-b")
    pool.mark_exhausted_and_rotate.return_value = other_entry

    recovered, _retried = recover_with_credential_pool(
        agent, status_code=429, has_retried_429=True,
        classified_reason=FailoverReason.rate_limit,
    )

    assert recovered is False, "must not report recovery when it swapped off the pinned account"
    agent._swap_credential.assert_not_called()


def test_pinned_session_rotation_to_the_same_account_still_recovers():
    """Re-selecting the SAME pinned entry (e.g. after a refresh) is not a swap."""
    agent, pool = _agent(pinned_credential_id="cred-a")
    pool.entries.return_value = []
    pool.current.return_value = SimpleNamespace(id="cred-a", runtime_api_key="key-a")
    same_entry = SimpleNamespace(id="cred-a", runtime_api_key="key-a")
    pool.mark_exhausted_and_rotate.return_value = same_entry

    recovered, _retried = recover_with_credential_pool(
        agent, status_code=429, has_retried_429=True,
        classified_reason=FailoverReason.rate_limit,
    )

    assert recovered is True
    agent._swap_credential.assert_called_once_with(same_entry)


def test_unpinned_session_still_auto_rotates():
    """No pin set (single-account chats, or a provider without a pin key) keeps
    the original auto-failover behaviour."""
    agent, pool = _agent(pinned_credential_id=None)
    pool.entries.return_value = []
    pool.current.return_value = SimpleNamespace(id="cred-a", runtime_api_key="key-a")
    other_entry = SimpleNamespace(id="cred-b", runtime_api_key="key-b")
    pool.mark_exhausted_and_rotate.return_value = other_entry

    recovered, _retried = recover_with_credential_pool(
        agent, status_code=429, has_retried_429=True,
        classified_reason=FailoverReason.rate_limit,
    )

    assert recovered is True
    agent._swap_credential.assert_called_once_with(other_entry)
