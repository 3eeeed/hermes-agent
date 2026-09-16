"""Dashboard-driven Anthropic OAuth: start -> user pastes code -> submit.

Anthropic has no device-code flow, so the account menu drives PKCE directly:
``/start`` returns an authorize URL and stashes the verifier server-side, the
user approves in a browser and pastes ``code#state`` back, and ``/submit``
exchanges it and appends a pool entry.

The previous dashboard implementation was removed in 0099f250c2 for leaking the
PKCE verifier as ``state`` and skipping the CSRF state check. These tests pin
the properties that made reinstating it safe: the verifier never leaves the
server, a failed exchange saves nothing, and ``add_account`` appends rather
than replaces.

``asyncio.run`` is used directly because pytest-asyncio is not installed in
this environment; ``@pytest.mark.asyncio`` would silently skip.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest
from fastapi import HTTPException

from hermes_cli import web_server_oauth as _web_server_oauth
from hermes_cli.web_models import OAuthSubmitBody
from hermes_cli.web_routers import oauth as _rt_oauth


class _Req:
    """Minimal stand-in for fastapi Request; token auth is monkeypatched off."""

    headers: Dict[str, str] = {}


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.setattr(_rt_oauth, "_require_token", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _clean_sessions():
    with _web_server_oauth._oauth_sessions_lock:
        _web_server_oauth._oauth_sessions.clear()
    yield
    with _web_server_oauth._oauth_sessions_lock:
        _web_server_oauth._oauth_sessions.clear()


@pytest.fixture
def _isolated_home(tmp_path, monkeypatch):
    """Point the auth store at a temp dir so tests never touch the real one."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _start(add_account: bool = False) -> Dict[str, Any]:
    return asyncio.run(
        _rt_oauth.start_oauth_login("anthropic", _Req(), add_account=add_account)
    )


def _submit(session_id: str, code: str, provider: str = "anthropic") -> Dict[str, Any]:
    return asyncio.run(
        _rt_oauth.submit_oauth_code(
            provider, OAuthSubmitBody(session_id=session_id, code=code), _Req()
        )
    )


def _stashed(session_id: str) -> Dict[str, Any]:
    with _web_server_oauth._oauth_sessions_lock:
        return dict(_web_server_oauth._oauth_sessions[session_id])


def test_start_returns_authorize_url_and_withholds_the_verifier(_isolated_home):
    """The PKCE verifier is the secret half of the grant — it must stay server-side."""
    resp = _start()

    assert resp["flow"] == "pkce"
    assert "claude.ai/oauth/authorize" in resp["auth_url"]
    assert "code_challenge_method=S256" in resp["auth_url"]
    assert "verifier" not in resp
    serialized = " ".join(str(v) for v in resp.values())
    assert _stashed(resp["session_id"])["verifier"] not in serialized


def test_each_start_uses_a_fresh_verifier_and_state(_isolated_home):
    """Replaying one verifier/state pair would let a callback be reused."""
    first, second = _stashed(_start()["session_id"]), _stashed(_start()["session_id"])
    assert first["verifier"] != second["verifier"]
    assert first["state"] != second["state"]


def test_submit_exchanges_with_the_server_side_verifier_and_saves(_isolated_home, monkeypatch):
    from agent.credential_pool import load_pool

    started = _start(add_account=True)
    stashed = _stashed(started["session_id"])
    seen: Dict[str, Any] = {}

    def _fake_complete(code, verifier, expected_state):
        seen.update(code=code, verifier=verifier, state=expected_state)
        return {"access_token": "at-new", "refresh_token": "rt-new"}

    monkeypatch.setattr(
        "agent.anthropic_credentials.complete_hermes_oauth_pure", _fake_complete
    )

    out = _submit(started["session_id"], f"the-code#{stashed['state']}")

    assert out["status"] == "success"
    assert seen["verifier"] == stashed["verifier"], "must use its own stashed verifier"
    assert seen["state"] == stashed["state"]
    entries = load_pool("anthropic").entries()
    assert [e.access_token for e in entries] == ["at-new"]


def test_add_account_appends_instead_of_replacing(_isolated_home, monkeypatch):
    """The whole point of '+': account #1 must survive adding account #2."""
    import uuid

    from agent.credential_pool import (
        AUTH_TYPE_OAUTH,
        SOURCE_MANUAL_DEVICE_CODE,
        PooledCredential,
        load_pool,
    )

    pool = load_pool("anthropic")
    pool.add_entry(
        PooledCredential(
            provider="anthropic",
            id=uuid.uuid4().hex[:6],
            label="first@example.com",
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=SOURCE_MANUAL_DEVICE_CODE,
            access_token="at-first",
            refresh_token="rt-first",
        )
    )

    started = _start(add_account=True)
    monkeypatch.setattr(
        "agent.anthropic_credentials.complete_hermes_oauth_pure",
        lambda *a, **k: {"access_token": "at-second", "refresh_token": "rt-second"},
    )
    _submit(started["session_id"], "c#s")

    tokens = {e.access_token for e in load_pool("anthropic").entries()}
    assert tokens == {"at-first", "at-second"}, "existing account was replaced"


def test_submit_rejects_a_session_started_for_another_provider(_isolated_home):
    started = _start()
    with pytest.raises(HTTPException) as err:
        _submit(started["session_id"], "c#s", provider="openai-codex")
    assert err.value.status_code == 400


def test_submit_rejects_an_unknown_session(_isolated_home):
    with pytest.raises(HTTPException) as err:
        _submit("does-not-exist", "c#s")
    assert err.value.status_code == 404


def test_a_rejected_code_saves_nothing_and_burns_the_session(_isolated_home, monkeypatch):
    """A failed exchange must not persist a credential, nor allow a retry."""
    from agent.credential_pool import load_pool

    started = _start(add_account=True)
    monkeypatch.setattr(
        "agent.anthropic_credentials.complete_hermes_oauth_pure",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("state did not match")),
    )

    with pytest.raises(HTTPException) as err:
        _submit(started["session_id"], "bad#state")
    assert err.value.status_code == 400
    assert not load_pool("anthropic").entries(), "a failed exchange saved a credential"

    # The session is burnt: the same verifier cannot be reused for another attempt.
    with pytest.raises(HTTPException) as again:
        _submit(started["session_id"], "another#try")
    assert again.value.status_code == 404
