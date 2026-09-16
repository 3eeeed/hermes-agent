"""The Anthropic PKCE flow must be drivable headlessly (dashboard), not just via stdin.

``run_hermes_oauth_login_pure`` is a terminal flow: it prints a URL and blocks on
``input()``. The desktop account menu needs the same grant without a terminal, so
the URL-building and code-exchange halves are exposed separately. These tests pin
the security-relevant behaviour of those halves: PKCE S256, and the CSRF state
guard that must reject a mismatched callback.
"""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest

from agent import anthropic_credentials as anth


def test_begin_returns_authorize_url_with_s256_challenge_for_its_verifier():
    begun = anth.begin_hermes_oauth_pure()
    query = parse_qs(urlparse(begun["authorize_url"]).query)

    assert query["code_challenge_method"] == ["S256"]
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(begun["verifier"].encode()).digest()
    ).decode().rstrip("=")
    assert query["code_challenge"] == [expected], "challenge does not match the verifier"
    assert query["state"] == [begun["state"]]
    assert query["client_id"] == [anth._OAUTH_CLIENT_ID]


def test_begin_is_unique_per_call():
    """A replayed verifier/state pair would let one callback be reused."""
    first, second = anth.begin_hermes_oauth_pure(), anth.begin_hermes_oauth_pure()
    assert first["verifier"] != second["verifier"]
    assert first["state"] != second["state"]


def test_complete_rejects_mismatched_state(monkeypatch):
    """CSRF guard (RFC 6749 section 10.12): a wrong state must never be exchanged."""
    called = []
    monkeypatch.setattr(
        anth, "_post_oauth_token", lambda *a, **k: called.append(1) or {"access_token": "x"}
    )
    with pytest.raises(ValueError, match="state"):
        anth.complete_hermes_oauth_pure("the-code#attacker-state", "verifier", "real-state")
    assert not called, "token exchange ran despite a state mismatch"


def test_complete_accepts_state_suffixed_code_and_returns_token_state(monkeypatch):
    seen = {}

    def _fake_post(data, content_type, timeout, what):
        import json as _json

        seen.update(_json.loads(data.decode()))
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}

    monkeypatch.setattr(anth, "_post_oauth_token", _fake_post)
    state = anth.complete_hermes_oauth_pure("the-code#real-state", "the-verifier", "real-state")

    assert seen["code"] == "the-code", "the '#state' suffix must be stripped before exchange"
    assert seen["code_verifier"] == "the-verifier"
    assert seen["grant_type"] == "authorization_code"
    assert state["access_token"] == "at"
    assert state["refresh_token"] == "rt"


def test_complete_accepts_bare_code_when_state_matches_out_of_band(monkeypatch):
    """Anthropic returns 'code#state'; a pasted bare code is still valid."""
    monkeypatch.setattr(
        anth,
        "_post_oauth_token",
        lambda *a, **k: {"access_token": "at", "refresh_token": "rt", "expires_in": 60},
    )
    assert anth.complete_hermes_oauth_pure("bare-code", "v", "")["access_token"] == "at"


def test_complete_rejects_a_response_without_an_access_token(monkeypatch):
    monkeypatch.setattr(anth, "_post_oauth_token", lambda *a, **k: {"error": "invalid_grant"})
    with pytest.raises(ValueError):
        anth.complete_hermes_oauth_pure("c#s", "v", "s")
