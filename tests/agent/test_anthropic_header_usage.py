"""Claude quota read from Messages response headers (setup tokens the usage route refuses)."""
from __future__ import annotations

import time

import httpx
import pytest

from agent import anthropic_header_usage as hu
from agent.account_usage import anthropic_header_usage_snapshot

TOKEN = "sk-ant-oat01-test-token"
FUTURE = int(time.time()) + 3600


def _headers(**claims):
    out = {}
    for claim, util in claims.items():
        out[f"anthropic-ratelimit-unified-{claim}-utilization"] = str(util)
        out[f"anthropic-ratelimit-unified-{claim}-reset"] = str(FUTURE)
    return out


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def test_session_week_and_fable_windows_from_headers():
    hu.record(TOKEN, _headers(**{"5h": 0.12, "7d": 0.24, "7d_oi": 0.03}))
    snap = anthropic_header_usage_snapshot(TOKEN, probe=False)
    assert snap.available
    assert [(w.label, round(w.used_percent), w.scope) for w in snap.windows] == [
        ("Current session", 12, "account"), ("Current week", 24, "account"), ("Fable weekly", 3, "model"),
    ]


def test_non_fable_reply_keeps_previous_fable_window():
    hu.record(TOKEN, _headers(**{"5h": 0.1, "7d": 0.2, "7d_oi": 0.05}))
    hu.record(TOKEN, _headers(**{"5h": 0.3, "7d": 0.25}))
    labels = {w.label: round(w.used_percent) for w in anthropic_header_usage_snapshot(TOKEN, probe=False).windows}
    assert labels == {"Current session": 30, "Current week": 25, "Fable weekly": 5}


def test_cache_never_stores_the_token(tmp_path):
    hu.record(TOKEN, _headers(**{"5h": 0.1}))
    assert TOKEN not in (tmp_path / "cache" / "anthropic_header_usage.json").read_text()


def test_elapsed_window_reads_zero():
    headers = _headers(**{"5h": 0.9})
    headers["anthropic-ratelimit-unified-5h-reset"] = str(int(time.time()) - 5)
    hu.record(TOKEN, headers)
    assert anthropic_header_usage_snapshot(TOKEN, probe=False).windows[0].used_percent == 0.0


def test_background_read_never_probes(monkeypatch):
    monkeypatch.setattr(hu, "probe", lambda *_a, **_k: pytest.fail("probe without menu open"))
    assert anthropic_header_usage_snapshot(TOKEN, probe=False) is None


def test_menu_probe_is_rate_limited(monkeypatch):
    calls = []

    def fake_post(self, url, headers=None, json=None):
        calls.append(json["model"])
        return httpx.Response(200, headers=_headers(**{"5h": 0.01, "7d": 0.24, "7d_oi": 0.0}))

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    first = anthropic_header_usage_snapshot(TOKEN, probe=True)
    anthropic_header_usage_snapshot(TOKEN, probe=True)
    assert calls == [hu.PROBE_MODEL]
    assert [w.label for w in first.windows] == ["Current session", "Current week", "Fable weekly"]
