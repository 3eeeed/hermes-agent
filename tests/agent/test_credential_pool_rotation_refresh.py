"""Rotation must hand back a *usable* credential, not a stale-token one.

Regression for the openai-codex three-account pool ("1" -> "2" -> "batal").

Observed failure: account "1" hits a genuine quota limit, the pool rotates to
account "2", and the very next request dies with ``401 token_expired`` two
seconds later.  Account "2" is healthy — its OAuth *access* token had simply
aged out while the account was idle.  ``mark_exhausted_and_rotate()`` selected
it with ``refresh=False``, so nothing ever minted a fresh access token; the
caller built the replacement client straight from the expired one.  The 401
was then attributed to account "2", which got benched, rotation advanced to
"batal" with the identical defect, and the pool reported "no available
entries" after a single lap.

These tests pin the contract: *whatever entry rotation returns must be
immediately usable*, and refresh/exhaustion decisions must stay scoped to the
one credential that actually failed.
"""

from __future__ import annotations

import base64
import json
import time

import pytest


CODEX = "openai-codex"


# ── helpers ────────────────────────────────────────────────────────────────


def _jwt(exp_offset_seconds: float, marker: str) -> str:
    """A decodable JWT whose ``exp`` sits *offset* seconds from now."""

    def _part(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    header = _part({"alg": "none", "typ": "JWT"})
    claims = _part({"exp": int(time.time() + exp_offset_seconds), "mkr": marker})
    return f"{header}.{claims}.sig"


def _expired(marker: str) -> str:
    return _jwt(-3600, marker)


def _fresh(marker: str) -> str:
    return _jwt(3600, marker)


def _entry(
    entry_id: str,
    label: str,
    priority: int,
    *,
    access_token: str,
    refresh_token: str,
    source: str = "manual:device_code",
    **overrides,
) -> dict:
    payload = {
        "id": entry_id,
        "label": label,
        "auth_type": "oauth",
        "priority": priority,
        "source": source,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "base_url": "https://chatgpt.com/backend-api/codex",
        "last_status": "ok",
    }
    payload.update(overrides)
    return payload


def _write_pool(hermes_home, entries: list[dict]) -> None:
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(
        json.dumps(
            {"version": 1, "credential_pool": {CODEX: entries}},
            indent=2,
        )
    )


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    """Isolated Hermes home with the host's Codex CLI import disabled."""
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "hermes_cli.auth._import_codex_cli_tokens", lambda: None,
    )
    return home


@pytest.fixture
def three_accounts(codex_home):
    """The operator's real shape: 1 -> 2 -> batal, all access tokens aged out.

    Every account carries a *distinct* access/refresh pair, matching three
    independently authenticated OAuth logins.
    """
    _write_pool(
        codex_home,
        [
            _entry(
                "acct1", "1", 0,
                access_token=_expired("a1"), refresh_token="refresh-a1",
            ),
            _entry(
                "acct2", "2", 1,
                access_token=_expired("a2"), refresh_token="refresh-a2",
            ),
            _entry(
                "batal", "batal", 2,
                access_token=_expired("a3"), refresh_token="refresh-a3",
            ),
        ],
    )
    return codex_home


class _RefreshRecorder:
    """Stand-in for the Codex token endpoint.

    Records every refresh_token it is handed so a test can prove *which*
    account was refreshed, and rotates the refresh token like the real
    single-use endpoint does.
    """

    def __init__(self, *, failing: set[str] | None = None, error=None):
        self.calls: list[str] = []
        self.failing = failing or set()
        self.error = error

    def __call__(self, access_token, refresh_token, **_kwargs):
        self.calls.append(refresh_token)
        if refresh_token in self.failing:
            raise self.error or RuntimeError("refresh rejected")
        marker = refresh_token.replace("refresh-", "")
        return {
            "access_token": _fresh(marker),
            "refresh_token": f"{refresh_token}-next",
            "last_refresh": "2026-08-29T00:00:00Z",
        }


def _install_refresh(monkeypatch, recorder) -> None:
    monkeypatch.setattr(
        "hermes_cli.auth.refresh_codex_oauth_pure", recorder,
    )


def _by_id(pool, entry_id):
    return next((e for e in pool.entries() if e.id == entry_id), None)


def _usable(entry) -> bool:
    """True when the entry's access token is not already expired."""
    from hermes_cli.auth import _codex_access_token_is_expiring

    return not _codex_access_token_is_expiring(entry.access_token, 0)


# ── the core regression ────────────────────────────────────────────────────


def test_quota_rotation_hands_back_a_usable_credential(
    three_accounts, monkeypatch
):
    """429 on "1" must rotate to a *refreshed* "2", not a stale-token "2".

    This is the bug: the returned entry carried an access token that had
    already expired, so the retried request 401'd immediately.
    """
    recorder = _RefreshRecorder()
    _install_refresh(monkeypatch, recorder)

    from agent.credential_pool import load_pool

    pool = load_pool(CODEX)

    nxt = pool.mark_exhausted_and_rotate(
        status_code=429,
        credential_id="acct1",
        api_key_hint=_by_id(pool, "acct1").access_token,
        error_context={"reason": "usage_limit_reached"},
        failure_reason="rate_limit",
    )

    assert nxt is not None, "rotation must find account 2"
    assert nxt.label == "2"
    assert _usable(nxt), (
        "rotation returned account 2 with an expired access token — the "
        "retried request will 401 with token_expired"
    )
    assert recorder.calls == ["refresh-a2"], (
        "only the rotated-to account may be refreshed; account 1's "
        "single-use refresh token must not be spent on a quota failure"
    )


def test_quota_rotation_does_not_reselect_the_exhausted_account(
    three_accounts, monkeypatch
):
    """Account "1" stays benched for its cooldown; it is not handed back."""
    _install_refresh(monkeypatch, _RefreshRecorder())

    from agent.credential_pool import load_pool

    pool = load_pool(CODEX)
    reset_at = time.time() + 3600
    pool.mark_exhausted_and_rotate(
        status_code=429,
        credential_id="acct1",
        error_context={"reason": "usage_limit_reached", "reset_at": reset_at},
        failure_reason="rate_limit",
    )

    acct1 = _by_id(pool, "acct1")
    assert acct1.last_status == "exhausted"
    assert acct1.last_error_reset_at == pytest.approx(reset_at, abs=2)

    for _ in range(3):
        assert pool.select().id != "acct1"


def test_second_exhaustion_falls_through_to_batal(three_accounts, monkeypatch):
    """"2" exhausted after "1" must land on "batal" — also refreshed."""
    recorder = _RefreshRecorder()
    _install_refresh(monkeypatch, recorder)

    from agent.credential_pool import load_pool

    pool = load_pool(CODEX)
    pool.mark_exhausted_and_rotate(
        status_code=429, credential_id="acct1", failure_reason="rate_limit",
    )
    nxt = pool.mark_exhausted_and_rotate(
        status_code=429, credential_id="acct2", failure_reason="rate_limit",
    )

    assert nxt is not None and nxt.label == "batal"
    assert _usable(nxt), "batal must be refreshed before it is handed back"
    assert "refresh-a3" in recorder.calls


# ── token_expired: refresh first, rotate only if refresh really fails ──────


def test_token_expired_with_working_refresh_keeps_the_same_account(
    three_accounts, monkeypatch
):
    """A refreshable 401 must retry the *same* account, not rotate away."""
    recorder = _RefreshRecorder()
    _install_refresh(monkeypatch, recorder)

    from agent.credential_pool import load_pool

    pool = load_pool(CODEX)
    refreshed = pool.try_refresh_matching(credential_id="acct1")

    assert refreshed is not None and refreshed.id == "acct1"
    assert _usable(refreshed)
    assert recorder.calls == ["refresh-a1"]
    assert _by_id(pool, "acct1").last_status == "ok"


def test_token_expired_with_dead_refresh_rotates_to_next_healthy(
    three_accounts, monkeypatch
):
    """Account "1"'s refresh_token is rejected -> use "2", refreshed."""
    from hermes_cli.auth import AuthError

    recorder = _RefreshRecorder(
        failing={"refresh-a1"},
        error=AuthError(
            "refresh rejected",
            provider=CODEX,
            code="codex_refresh_failed",
            relogin_required=True,
        ),
    )
    _install_refresh(monkeypatch, recorder)

    from agent.credential_pool import load_pool

    pool = load_pool(CODEX)
    assert pool.try_refresh_matching(credential_id="acct1") is None

    nxt = pool.mark_exhausted_and_rotate(
        status_code=401,
        credential_id="acct1",
        error_context={"reason": "token_expired"},
        failure_reason="auth",
    )

    assert nxt is not None and nxt.label == "2"
    assert _usable(nxt), (
        "the replacement account must be refreshed before the retry, "
        "otherwise its own stale token 401s and the pool empties"
    )
    # A refreshable 401 is not terminal — account 1 cools down, it is not DEAD.
    assert _by_id(pool, "acct1").last_status == "exhausted"


def test_rotation_skips_a_candidate_whose_refresh_token_is_dead(
    three_accounts, monkeypatch
):
    """"1" quota-limits, "2"'s refresh token is revoked -> land on "batal".

    The middle account must not be handed back with the stale token that just
    failed to refresh, and it must not take the other accounts down with it.
    """
    from hermes_cli.auth import AuthError

    recorder = _RefreshRecorder(
        failing={"refresh-a2"},
        error=AuthError(
            "refresh rejected",
            provider=CODEX,
            code="codex_refresh_failed",
            relogin_required=True,
        ),
    )
    _install_refresh(monkeypatch, recorder)

    from agent.credential_pool import load_pool

    pool = load_pool(CODEX)
    nxt = pool.mark_exhausted_and_rotate(
        status_code=429, credential_id="acct1", failure_reason="rate_limit",
    )

    assert nxt is not None and nxt.label == "batal", (
        "account 2 could not refresh — rotation must step over it"
    )
    assert _usable(nxt)
    # A terminally-rejected refresh grant (relogin_required) means the login
    # itself is gone, not a transient quota bench — main's fix for #113023:
    # silently exhausting it replayed the dead token every hour at DEBUG with
    # no visible trace that the account needs re-authentication.
    assert _by_id(pool, "acct2").last_status == "dead"


def test_token_invalidated_kills_only_that_credential(
    three_accounts, monkeypatch
):
    """A genuinely revoked account goes DEAD; the others stay healthy."""
    recorder = _RefreshRecorder()
    _install_refresh(monkeypatch, recorder)

    from agent.credential_pool import load_pool

    pool = load_pool(CODEX)
    nxt = pool.mark_exhausted_and_rotate(
        status_code=401,
        credential_id="acct1",
        error_context={"reason": "token_invalidated"},
        failure_reason="auth",
    )

    assert _by_id(pool, "acct1").last_status == "dead"
    assert _by_id(pool, "acct2").last_status != "dead"
    assert _by_id(pool, "batal").last_status != "dead"
    assert nxt is not None and nxt.label == "2"
    assert _usable(nxt)


# ── singleton isolation ────────────────────────────────────────────────────


def test_singleton_device_code_never_overwrites_a_manual_entry(
    codex_home, monkeypatch
):
    """``providers.openai-codex`` must not bleed into ``manual:*`` entries.

    The singleton is one account; the pool holds three.  Seeding or syncing
    the singleton over a manual entry would silently collapse the pool onto a
    single credential and revive a benched account with someone else's token.
    """
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {
                    CODEX: {
                        "tokens": {
                            "access_token": _fresh("singleton"),
                            "refresh_token": "refresh-singleton",
                        },
                        "last_refresh": "2026-08-29T00:00:00Z",
                    }
                },
                "credential_pool": {
                    CODEX: [
                        _entry(
                            "acct1", "1", 0,
                            access_token=_expired("a1"),
                            refresh_token="refresh-a1",
                            last_status="exhausted",
                            last_status_at=time.time(),
                            last_error_code=429,
                            last_error_reason="usage_limit_reached",
                            last_error_reset_at=time.time() + 3600,
                        ),
                        _entry(
                            "acct2", "2", 1,
                            access_token=_expired("a2"),
                            refresh_token="refresh-a2",
                        ),
                    ]
                },
            },
            indent=2,
        )
    )
    _install_refresh(monkeypatch, _RefreshRecorder())

    from agent.credential_pool import load_pool

    pool = load_pool(CODEX)

    manual_refresh_tokens = {
        e.refresh_token for e in pool.entries()
        if e.source.startswith("manual:")
    }
    assert "refresh-singleton" not in manual_refresh_tokens, (
        "singleton tokens were written over a manual pool entry"
    )
    # The benched manual account must stay benched — the singleton's fresh
    # tokens say nothing about *that* account's quota.
    assert _by_id(pool, "acct1").last_status == "exhausted"


# ── caller level: the *same* request must be retried on account "2" ────────


class _FakeAgent:
    """The slice of ``AIAgent`` that ``recover_with_credential_pool`` touches."""

    log_prefix = ""
    provider = CODEX
    _fallback_activated = False

    def __init__(self, pool, entry):
        self._credential_pool = pool
        self.api_key = entry.access_token
        self.base_url = entry.base_url
        self._credential_pool_entry_id = entry.id
        self.swaps: list = []

    def _swap_credential(self, entry):
        self.swaps.append(entry)
        self.api_key = entry.access_token
        self._credential_pool_entry_id = entry.id

    def _is_entitlement_failure(self, *_args, **_kwargs):
        return False


def test_same_request_is_retried_on_account_two_after_a_quota_limit(
    three_accounts, monkeypatch
):
    """End-to-end through the recovery helper, with a real pool.

    Account "1" hits a genuine usage limit; the identical request payload must
    go back out on account "2" using a live token — no re-auth, no 401.
    """
    recorder = _RefreshRecorder()
    _install_refresh(monkeypatch, recorder)

    from agent.credential_pool import load_pool
    from agent.agent_runtime_helpers import recover_with_credential_pool
    from agent.error_classifier import FailoverReason

    pool = load_pool(CODEX)
    acct1 = _by_id(pool, "acct1")
    agent = _FakeAgent(pool, acct1)

    request = {"model": "gpt-5-codex", "messages": [{"role": "user", "content": "hi"}]}
    sent: list[tuple[str, dict]] = []
    sent.append((agent.api_key, dict(request)))

    recovered, has_retried = recover_with_credential_pool(
        agent,
        status_code=429,
        has_retried_429=True,  # second consecutive 429 -> rotate
        classified_reason=FailoverReason.rate_limit,
        error_context={
            "reason": "usage_limit_reached",
            "message": "You've hit your usage limit.",
        },
    )

    assert recovered is True, "a quota limit on account 1 must be recoverable"
    assert has_retried is False
    assert [e.label for e in agent.swaps] == ["2"]

    # The conversation loop's ``continue`` re-sends the untouched payload.
    sent.append((agent.api_key, dict(request)))

    assert sent[0][1] == sent[1][1], "the retried request must be byte-identical"
    assert sent[1][0] != sent[0][0], "the retry must use a different account"
    assert sent[1][0] == _by_id(pool, "acct2").access_token
    assert not _codex_expiring(sent[1][0]), (
        "retry went out on account 2's expired access token — this is the "
        "401 token_expired the operator saw"
    )
    assert _by_id(pool, "acct1").last_status == "exhausted"
    assert _by_id(pool, "acct1").last_error_reason == "usage_limit_reached"


def _codex_expiring(token: str) -> bool:
    from hermes_cli.auth import _codex_access_token_is_expiring

    return _codex_access_token_is_expiring(token, 0)
