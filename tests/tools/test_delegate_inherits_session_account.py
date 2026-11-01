"""A chat pinned to one pool account (desktop account menu) bills its delegate_task workers to
that same account: the worker leases exactly the parent's pinned entry, never the pool's priority-0
account. An explicit Delegate-lane account still wins over the inherited one."""
import pytest

from agent.credential_pool import AUTH_TYPE_API_KEY, CredentialPool, PooledCredential
from tools.delegate_tool_child_run import _lease_child_credential

KEY_A = "sk-ant-api03-" + "a" * 40
KEY_B = "sk-ant-api03-" + "b" * 40


def _entry(cid, key, priority):
    return PooledCredential(provider="anthropic", id=cid, label=cid, auth_type=AUTH_TYPE_API_KEY,
                            priority=priority, source="manual", access_token=key)


@pytest.fixture
def parent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  default: claude-sonnet-4-6\n  provider: anthropic\n",
                                          encoding="utf-8")
    from run_agent import AIAgent
    from tools import delegate_tool as dt
    import tools.delegate_tool_config as dtc
    monkeypatch.setattr(dt, "_load_config", lambda: {})
    monkeypatch.setattr(dtc, "_load_config", lambda: {})
    monkeypatch.setattr(CredentialPool, "_persist", lambda self, **kw: None)
    pool = CredentialPool("anthropic", [_entry("acc-a", KEY_A, 0), _entry("acc-b", KEY_B, 1)])
    agent = AIAgent(session_id="p", api_key=KEY_B, base_url="https://api.anthropic.com", provider="anthropic",
                    api_mode="anthropic_messages", model="claude-sonnet-4-6", platform="cli", quiet_mode=True,
                    skip_context_files=True, skip_memory=True, save_trajectories=False,
                    enabled_toolsets=["file"], credential_pool=pool)
    agent._credential_pool = pool
    agent._session_pinned_credential_id = "acc-b"  # desktop account menu picked B
    yield agent
    agent.close()


def _build(parent, **kw):
    from tools import delegate_tool as dt
    return dt._build_child_agent(task_index=0, goal="g", context=None, toolsets=["file"], model=None,
                                 max_iterations=2, task_count=1, parent_agent=parent, **kw)


def test_worker_of_a_pinned_chat_leases_the_pinned_account(parent):
    child = _build(parent)
    try:
        pool, leased = _lease_child_credential(child)
        assert leased == "acc-b"
        assert child.api_key == KEY_B
        assert getattr(child, "_session_pinned_credential_id", None) == "acc-b"  # never auto-rotates away
        pool.release_lease(leased)
    finally:
        child.close()

    parent._session_pinned_credential_id = None  # unpinned chat: pool order (A) as before
    child = _build(parent)
    try:
        assert _lease_child_credential(child)[1] == "acc-a"
    finally:
        child.close()


def test_explicit_lane_account_wins_over_the_chat_pin(parent):
    child = _build(parent, routing_cfg={"credential_id": "acc-a"})
    try:
        assert _lease_child_credential(child)[1] == "acc-a"
        assert child.api_key == KEY_A
    finally:
        child.close()
