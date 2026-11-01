"""Delegate mode (Desktop "Delegate" composer mode): a lane worker spawned from a chat pinned to one
pool account must send that account's key on its first request, not the pool's priority-0 key.

Drives the real path end to end: ``orchestrator_turn`` arms the lane routes exactly as the Desktop
turn does, ``AIAgent._dispatch_delegate_task`` resolves the lane (``routing_for_tasks``) and hands it
to ``delegate_task``; the real ``_run_single_child`` leases the credential and runs the child's
``run_conversation``. The only seam is the request-client factory, which records the key the child
would authenticate its first Messages request with (after the lease AND the per-request Anthropic
credential refresh), then fails the call so nothing leaves the process.
"""
import json

import pytest

from agent.credential_pool import AUTH_TYPE_API_KEY, PooledCredential

KEY_A = "test-key-account-a-" + "a" * 32  # pool priority 0 (the machine default)
KEY_B = "test-key-account-b-" + "b" * 32  # the account the user picked for this chat
MODEL = "claude-sonnet-4-6"


class _StopRequest(Exception):
    """Raised by the fake client factory: the request never leaves the process."""


def _row(cid, key, priority):
    return PooledCredential(provider="anthropic", id=cid, label=cid, auth_type=AUTH_TYPE_API_KEY,
                            priority=priority, source="manual", access_token=key).to_dict()


@pytest.fixture
def parent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "config.yaml").write_text(
        f"model:\n  default: {MODEL}\n  provider: anthropic\nauth:\n  adopt_external_logins: false\n",
        encoding="utf-8")
    (tmp_path / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {
        "anthropic": [_row("acc-a", KEY_A, 0), _row("acc-b", KEY_B, 1)]}}), encoding="utf-8")
    import tools.delegate_tool as dt
    import tools.delegate_tool_config as dtc
    monkeypatch.setattr(dt, "_load_config", lambda: {"max_iterations": 2})
    monkeypatch.setattr(dtc, "_load_config", lambda: {"max_iterations": 2})
    from agent.credential_pool import load_pool
    from run_agent import AIAgent
    pool = load_pool("anthropic")
    agent = AIAgent(session_id="p", api_key=KEY_A, base_url="https://api.anthropic.com", provider="anthropic",
                    api_mode="anthropic_messages", model=MODEL, platform="desktop", quiet_mode=True,
                    skip_context_files=True, skip_memory=True, save_trajectories=False,
                    enabled_toolsets=["file"], credential_pool=pool)
    # What tui_gateway's _apply_session_credential_selection does for a chat pinned to B.
    agent._session_pinned_credential_id = "acc-b"
    assert agent._swap_credential(next(e for e in pool.entries() if e.id == "acc-b"))
    yield agent
    agent.close()


def _first_request_keys(parent, monkeypatch, worker):
    """Run one Delegate-mode delegate_task through the lane; return the keys the worker sent."""
    from agent.client_lifecycle import ClientLifecycleMixin
    from agent.delegate_orchestrator import orchestrator_turn
    sent = []

    def record_and_stop(self, key):
        if getattr(self, "_delegate_depth", 0):  # only the worker builds request clients here
            sent.append(key[1])
        raise _StopRequest("no network in tests")

    monkeypatch.setattr(ClientLifecycleMixin, "_build_anthropic_client_for_key", record_and_stop)
    monkeypatch.setattr("agent.delegate_orchestrator.before_dispatch", lambda agent: None)
    monkeypatch.setattr("agent.delegate_orchestrator.after_dispatch", lambda agent, result: result)
    options = {"workers": [{"lane": "research", "provider": "anthropic", "model": MODEL, "effort": "medium",
                            **worker}]}
    with orchestrator_turn(parent, "task-1", options):
        out = parent._dispatch_delegate_task({"goal": "look something up", "background": False})
    assert sent, out
    return sent


def test_lane_worker_of_a_pinned_chat_bills_the_pinned_account(parent, monkeypatch):
    assert set(_first_request_keys(parent, monkeypatch, {})) == {KEY_B}

    parent._session_pinned_credential_id = None  # an unpinned chat keeps pool order
    assert _first_request_keys(parent, monkeypatch, {})[0] == KEY_A


def test_explicit_lane_account_still_wins(parent, monkeypatch):
    assert _first_request_keys(parent, monkeypatch, {"credential_id": "acc-a"})[0] == KEY_A
