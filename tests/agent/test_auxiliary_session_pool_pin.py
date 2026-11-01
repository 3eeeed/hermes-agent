"""Auxiliary calls made for a chat pinned to one pool account (desktop account menu) bill that
account, not the pool's priority-0 entry; with nothing bound they keep using pool order."""
import time

import pytest

import agent.anthropic_credentials as anth_cred
import agent.auxiliary_client as aux
from hermes_cli.auth import write_credential_pool


def _row(cid, token, priority):
    expires = time.time() + 3600
    return {"id": cid, "label": cid, "auth_type": "oauth", "priority": priority, "source": "manual",
            "access_token": token, "refresh_token": f"rt-{cid}", "expires_at": expires,
            "expires_at_ms": int(expires * 1000)}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(aux, "_client_cache", {})
    monkeypatch.setattr(anth_cred, "read_claude_code_credentials", lambda: None)
    return tmp_path / "hermes"


def test_anthropic_aux_client_follows_the_bound_session_account(home):
    write_credential_pool("anthropic", [_row("acc-a", "tok-a", 0), _row("acc-b", "tok-b", 1)])

    client, _ = aux._get_cached_client("anthropic", "claude-haiku-4-5")
    assert client.api_key == "tok-a"  # nothing bound: pool order

    with aux.scoped_session_pool_credential("anthropic", "acc-b"):
        pinned, _ = aux._get_cached_client("anthropic", "claude-haiku-4-5")
    assert pinned.api_key == "tok-b"
    # The binding ends with its scope: an unbound context is back on pool order (no cache bleed).
    again, _ = aux._get_cached_client("anthropic", "claude-haiku-4-5")
    assert again.api_key == "tok-a"


def test_codex_aux_token_follows_the_bound_session_account(home):
    write_credential_pool("openai-codex", [_row("cx-a", "cxtok-a", 0), _row("cx-b", "cxtok-b", 1)])
    assert aux._resolve_codex_credential_and_base()[0] == "cxtok-a"
    with aux.scoped_session_pool_credential("openai-codex", "cx-b"):
        assert aux._resolve_codex_credential_and_base()[0] == "cxtok-b"
    # A pin for another provider never selects this provider's entry.
    with aux.scoped_session_pool_credential("anthropic", "cx-b"):
        assert aux._resolve_codex_credential_and_base()[0] == "cxtok-a"
