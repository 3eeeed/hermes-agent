import concurrent.futures
import contextvars
import threading
import time
from datetime import datetime, timezone, UTC

import pytest

from agent.account_usage import (
    AccountUsageSnapshot,
    AccountUsageWindow,
    _fetch_portal_account,
    fetch_account_usage,
    render_account_usage_lines,
)
from agent.billing_usage import fetch_nous_account as _billing_fetch_nous_account
from providers.base import ProviderProfile


class _UsageProfile(ProviderProfile):
    def __init__(self, snapshot=None, error=None, name="plugin-usage"):
        super().__init__(name=name)
        self.snapshot = snapshot
        self.error = error
        self.calls = 0

    def fetch_account_usage(self, *, base_url=None, api_key=None):
        self.calls += 1
        if self.error:
            raise self.error
        return self.snapshot


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, headers=None):
        return _Response(self._payload)


class _RoutingClient:
    def __init__(self, payloads):
        self._payloads = payloads

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, headers=None):
        return _Response(self._payloads[url])


def test_fetch_account_usage_codex(monkeypatch):
    monkeypatch.setattr(
        "agent.account_usage.resolve_codex_runtime_credentials",
        lambda refresh_if_expiring=True: {
            "provider": "openai-codex",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "access-token",
        },
    )
    monkeypatch.setattr(
        "agent.account_usage._read_codex_tokens",
        lambda: {"tokens": {"account_id": "acct_123"}},
    )
    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=15.0: _Client(
            {
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 15,
                        "reset_at": 1_900_000_000,
                        "limit_window_seconds": 18000,
                    },
                    "secondary_window": {
                        "used_percent": 40,
                        "reset_at": 1_900_500_000,
                        "limit_window_seconds": 604800,
                    },
                },
                "credits": {"has_credits": True, "balance": 12.5},
            }
        ),
    )

    snapshot = fetch_account_usage("openai-codex")

    assert snapshot is not None
    assert snapshot.plan == "Pro"
    assert len(snapshot.windows) == 2
    assert snapshot.windows[0].label == "Session"
    assert snapshot.windows[0].used_percent == 15.0
    assert snapshot.windows[0].reset_at == datetime.fromtimestamp(1_900_000_000, tz=UTC)
    assert "Credits balance: $12.50" in snapshot.details


def _register_profile(monkeypatch, profile):
    import providers

    monkeypatch.setattr(providers, "_REGISTRY", dict(providers._REGISTRY))
    monkeypatch.setattr(providers, "_ALIASES", dict(providers._ALIASES))
    monkeypatch.setattr(providers, "_PROVIDER_LIST_CACHE", None)  # else the fixture profile outlives the test
    providers.register_provider(profile)


def test_fetch_account_usage_reaches_registered_plugin_profile_and_fails_open(monkeypatch):
    """A profile registered through the public registry (as a plugin does) feeds /usage; a profile
    without the hook, or one whose hook raises, is indistinguishable from today's empty block."""
    snapshot = AccountUsageSnapshot(
        provider="plugin-usage", source="plugin", fetched_at=datetime.now(UTC),
        details=("Credit: 10/100",),
    )
    profile = _UsageProfile(snapshot)
    _register_profile(monkeypatch, profile)
    _register_profile(monkeypatch, ProviderProfile(name="plugin-silent"))
    _register_profile(monkeypatch, _UsageProfile(error=RuntimeError("nope"), name="plugin-broken"))

    assert fetch_account_usage("plugin-usage", base_url="https://plugin.test", api_key="key") is snapshot
    assert profile.calls == 1
    assert fetch_account_usage("plugin-silent") is None
    assert fetch_account_usage("plugin-broken") is None


def test_fetch_account_usage_prefers_builtin_fetcher_over_profile(monkeypatch):
    builtin = AccountUsageSnapshot(
        provider="openrouter", source="builtin", fetched_at=datetime.now(UTC),
    )
    profile = _UsageProfile(
        AccountUsageSnapshot(provider="openrouter", source="plugin", fetched_at=datetime.now(UTC)),
        name="openrouter",
    )
    monkeypatch.setattr("agent.account_usage._USAGE_FETCHERS", {"openrouter": lambda base_url, api_key: builtin})
    _register_profile(monkeypatch, profile)

    assert fetch_account_usage("openrouter") is builtin
    assert profile.calls == 0


def test_fetch_account_usage_openrouter_uses_limit_remaining_and_ignores_deprecated_rate_limit(monkeypatch):
    monkeypatch.setattr(
        "agent.account_usage.resolve_runtime_provider",
        lambda requested, explicit_base_url=None, explicit_api_key=None: {
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-test",
        },
    )
    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=10.0: _RoutingClient(
            {
                "https://openrouter.ai/api/v1/credits": {
                    "data": {"total_credits": 300.0, "total_usage": 10.92}
                },
                "https://openrouter.ai/api/v1/key": {
                    "data": {
                        "limit": 100.0,
                        "limit_remaining": 70.0,
                        "limit_reset": "monthly",
                        "usage": 12.5,
                        "usage_daily": 0.5,
                        "usage_weekly": 2.0,
                        "usage_monthly": 8.0,
                        "rate_limit": {"requests": -1, "interval": "10s"},
                    }
                },
            }
        ),
    )

    snapshot = fetch_account_usage("openrouter")

    assert snapshot is not None
    assert snapshot.windows == (
        AccountUsageWindow(
            label="API key quota",
            used_percent=30.0,
            detail="$70.00 of $100.00 remaining • resets monthly",
        ),
    )
    assert "Credits balance: $289.08" in snapshot.details
    assert "API key usage: $12.50 total • $0.50 today • $2.00 this week • $8.00 this month" in snapshot.details
    assert all("-1 requests / 10s" not in line for line in render_account_usage_lines(snapshot))


def test_fetch_account_usage_openrouter_omits_quota_window_when_key_has_no_limit(monkeypatch):
    monkeypatch.setattr(
        "agent.account_usage.resolve_runtime_provider",
        lambda requested, explicit_base_url=None, explicit_api_key=None: {
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-test",
        },
    )
    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=10.0: _RoutingClient(
            {
                "https://openrouter.ai/api/v1/credits": {
                    "data": {"total_credits": 100.0, "total_usage": 25.5}
                },
                "https://openrouter.ai/api/v1/key": {
                    "data": {
                        "limit": None,
                        "limit_remaining": None,
                        "usage": 25.5,
                        "usage_daily": 1.25,
                        "usage_weekly": 4.5,
                        "usage_monthly": 18.0,
                    }
                },
            }
        ),
    )

    snapshot = fetch_account_usage("openrouter")

    assert snapshot is not None
    assert snapshot.windows == ()
    assert "Credits balance: $74.50" in snapshot.details
    assert "API key usage: $25.50 total • $1.25 today • $4.50 this week • $18.00 this month" in snapshot.details


def test_plugin_usage_hook_is_bounded_and_fails_open(monkeypatch):
    """A plugin hook that overruns the shared deadline yields None within deadline+1 s on every surface
    (gateway/TUI call ``fetch_account_usage`` with no bound of their own); built-in fetchers are untouched."""
    import threading
    import time

    from agent import account_usage

    started = threading.Event()

    class _Hang(ProviderProfile):
        def fetch_account_usage(self, *, base_url=None, api_key=None):
            started.set()
            time.sleep(5)
            return AccountUsageSnapshot(provider=self.name, source="late", fetched_at=datetime.now(UTC))

    _register_profile(monkeypatch, _Hang(name="plugin-hang"))
    monkeypatch.setattr(account_usage, "PLUGIN_USAGE_HOOK_DEADLINE_S", 0.3)
    builtin_calls = []
    monkeypatch.setattr(account_usage, "_USAGE_FETCHERS",
                        {"openrouter": lambda base_url, api_key: builtin_calls.append(1)})

    t0 = time.monotonic()
    assert account_usage.fetch_account_usage("plugin-hang") is None
    assert time.monotonic() - t0 < 1.3 and started.is_set()
    account_usage.fetch_account_usage("openrouter")
    assert builtin_calls == [1]


def test_plugin_usage_hook_failure_never_reaches_threading_excepthook(monkeypatch):
    """A raising hook fails open in the caller — it must not die on a worker thread, where
    ``threading.excepthook`` prints a traceback into every ``/usage`` surface."""
    import threading

    from agent import account_usage

    class _Boom(ProviderProfile):
        def fetch_account_usage(self, *, base_url=None, api_key=None):
            raise RuntimeError("boom from plugin")

    _register_profile(monkeypatch, _Boom(name="plugin-boom"))
    monkeypatch.setattr(account_usage, "_USAGE_FETCHERS", {})
    escaped = []
    monkeypatch.setattr(threading, "excepthook", lambda args: escaped.append(args.exc_value))

    assert account_usage.fetch_account_usage("plugin-boom") is None
    for t in threading.enumerate():
        if t is not threading.current_thread() and "account-usage" in t.name:
            t.join(2)
    assert escaped == []


@pytest.mark.parametrize("fetch", [_fetch_portal_account, _billing_fetch_nous_account])
def test_fetch_portal_account_is_wall_clock_bounded(monkeypatch, fetch):
    """A portal that accepts the connection but never answers must release the
    caller at ``timeout``, not when the wedged worker finishes on its own
    (``Executor.__exit__`` used to join it via ``shutdown(wait=True)``) — on the
    /usage path and the /billing path alike (#115982)."""
    release = threading.Event()

    def hanging_portal_fetch(*, force_fresh):
        release.wait(timeout=30)
        return object()

    monkeypatch.setattr(
        "hermes_cli.nous_account.get_nous_portal_account_info", hanging_portal_fetch
    )
    started = time.monotonic()
    try:
        with pytest.raises(concurrent.futures.TimeoutError):
            fetch(timeout=0.5)
    finally:
        release.set()
    assert time.monotonic() - started < 10


def test_fetch_portal_account_returns_value_and_keeps_caller_context(monkeypatch):
    marker = contextvars.ContextVar("portal_fetch_test_marker", default="unset")
    sentinel = object()
    seen = {}

    def probing_portal_fetch(*, force_fresh):
        seen["force_fresh"] = force_fresh
        seen["marker"] = marker.get()
        return sentinel

    monkeypatch.setattr(
        "hermes_cli.nous_account.get_nous_portal_account_info", probing_portal_fetch
    )
    token = marker.set("profile-scope")
    try:
        assert _fetch_portal_account(timeout=5) is sentinel
    finally:
        marker.reset(token)
    assert seen == {"force_fresh": True, "marker": "profile-scope"}


class _CapturingClient:
    """Records the Authorization header so per-account attribution is assertable."""

    def __init__(self, payload, seen):
        self._payload, self._seen = payload, seen

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, headers=None):
        self._seen.append((headers or {}).get("Authorization", ""))
        return _Response(self._payload)


_ANTHROPIC_USAGE_PAYLOAD = {
    "five_hour": {"utilization": 0.25, "resets_at": "2026-01-01T00:00:00Z"},
}


def test_anthropic_usage_uses_passed_api_key_not_ambient_token(monkeypatch):
    """Per-account usage must bill the account it was asked about.

    The pool passes each entry's own token as ``api_key``; ignoring it and
    falling back to resolve_anthropic_token() makes every Claude account in
    the pool report the *same* (first/ambient) account's usage.
    """
    seen: list[str] = []
    # Ambient/global token — deliberately different from the per-entry token.
    monkeypatch.setattr("agent.account_usage.resolve_anthropic_token", lambda: "ambient-oauth-token")
    monkeypatch.setattr("agent.account_usage._is_oauth_token", lambda tok: True)
    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=15.0: _CapturingClient(_ANTHROPIC_USAGE_PAYLOAD, seen),
    )

    fetch_account_usage("anthropic", base_url=None, api_key="per-entry-token")

    assert seen, "usage endpoint was never called"
    assert seen[0] == "Bearer per-entry-token", (
        f"usage was billed to the wrong account: {seen[0]!r}"
    )


# Real shape of a Team account that has a model-scoped weekly cap. Claude's own
# usage page renders three bars from this: session, "All models", and "Fable".
_ANTHROPIC_SCOPED_LIMIT_PAYLOAD = {
    "five_hour": {"utilization": 8.0, "resets_at": "2026-09-17T18:30:00+00:00"},
    "seven_day": {"utilization": 88.0, "resets_at": "2026-09-17T21:00:00+00:00"},
    "limits": [
        {"kind": "session", "group": "session", "percent": 8, "scope": None},
        {"kind": "weekly_all", "group": "weekly", "percent": 88, "scope": None},
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 100,
            "resets_at": "2026-09-17T20:59:59+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
        },
    ],
}


def test_anthropic_usage_reports_model_scoped_weekly_cap(monkeypatch):
    """A per-model cap exists only in ``limits`` and must not be dropped.

    It can be the limit actually throttling the account (100% while the
    all-models window still reads 88%), so reporting the top-level keys alone
    tells the user they have headroom they do not have.
    """
    monkeypatch.setattr("agent.account_usage._is_oauth_token", lambda tok: True)
    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=15.0: _Client(_ANTHROPIC_SCOPED_LIMIT_PAYLOAD),
    )

    snapshot = fetch_account_usage("anthropic", base_url=None, api_key="tok")

    assert snapshot is not None
    by_label = {window.label: window for window in snapshot.windows}
    assert "Fable weekly" in by_label, f"model-scoped cap dropped: {sorted(by_label)}"
    assert by_label["Fable weekly"].used_percent == 100.0
    assert by_label["Fable weekly"].reset_at is not None
    # The unscoped entries duplicate the top-level windows and must not double up.
    assert len(snapshot.windows) == 3
    assert by_label["Current week"].used_percent == 88.0


def test_anthropic_usage_without_scoped_limits_is_unchanged(monkeypatch):
    """An account with no per-model cap keeps exactly its top-level windows."""
    monkeypatch.setattr("agent.account_usage._is_oauth_token", lambda tok: True)
    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=15.0: _Client({
            "five_hour": {"utilization": 0.25, "resets_at": "2026-01-01T00:00:00Z"},
            "limits": [{"kind": "session", "group": "session", "percent": 0.25, "scope": None}],
        }),
    )

    snapshot = fetch_account_usage("anthropic", base_url=None, api_key="tok")

    assert snapshot is not None
    assert [window.label for window in snapshot.windows] == ["Current session"]
    assert snapshot.windows[0].used_percent == 0.25


def test_anthropic_usage_formats_minor_unit_spend(monkeypatch):
    """Anthropic's spend amounts are minor currency units, not whole dollars."""
    monkeypatch.setattr("agent.account_usage._is_oauth_token", lambda tok: True)
    monkeypatch.setattr(
        "agent.account_usage.httpx.Client",
        lambda timeout=15.0: _Client({
            "spend": {
                "enabled": True,
                "used": {"amount_minor": 154, "currency": "USD", "exponent": 2},
                "limit": {"amount_minor": 100, "currency": "USD", "exponent": 2},
            },
            "extra_usage": {
                "is_enabled": True,
                "used_credits": 154.0,
                "monthly_limit": 100,
                "currency": "USD",
                "decimal_places": 2,
            },
        }),
    )

    snapshot = fetch_account_usage("anthropic", base_url=None, api_key="tok")

    assert snapshot is not None
    assert snapshot.details == ("Extra usage: 1.54 / 1.00 USD",)
