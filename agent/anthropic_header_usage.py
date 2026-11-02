"""Claude subscription quota read from Messages response headers.

Long-lived ``setup-token`` OAuth tokens are refused by ``/api/oauth/usage``
(HTTP 429 on every call), so the account menu showed "Usage unavailable" for
them. Every Messages response still carries the same numbers as
``anthropic-ratelimit-unified-*`` headers: ``5h`` (session), ``7d`` (week)
and, on responses for a model with its own weekly cap, ``7d_oi`` (the Fable
cap the usage page lists as a scoped ``limits`` entry).

Snapshots are cached per token fingerprint in ``<HERMES_HOME>/cache`` so the
agent process that saw a chat response and the dashboard process serving the
account menu share them. The file holds only percentages, reset times and a
SHA-256 prefix of the token; never the token.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Optional

import httpx

logger = logging.getLogger(__name__)

_PREFIX = "anthropic-ratelimit-unified-"
# (header claim, window label). Labels match the usage-page fetcher so the
# Desktop renders both sources identically.
_CLAIMS = (("5h", "Current session"), ("7d", "Current week"), ("7d_oi", "Fable weekly"))
# A Fable request is the one that returns the scoped ``7d_oi`` claim.
PROBE_MODEL = "claude-fable-5"
# Menu re-opens inside this window reuse the last snapshot instead of probing.
PROBE_MIN_INTERVAL_SECONDS = 120
_CACHE_NAME = "anthropic_header_usage.json"
_lock = threading.Lock()


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def parse_unified_headers(headers: Optional[Mapping[str, str]]) -> Optional[dict[str, Any]]:
    """Return ``{"windows": [...], "captured_at": ts}`` or None when no quota headers are present."""
    if not headers:
        return None
    lowered = {str(k).lower(): v for k, v in headers.items()}
    windows = []
    for claim, label in _CLAIMS:
        raw = lowered.get(f"{_PREFIX}{claim}-utilization")
        if raw is None:
            continue
        try:
            used = float(raw) * 100.0
        except (TypeError, ValueError):
            continue
        reset = lowered.get(f"{_PREFIX}{claim}-reset")
        try:
            reset_ts = int(reset) if reset is not None else None
        except (TypeError, ValueError):
            reset_ts = None
        windows.append({"claim": claim, "label": label, "used_percent": used, "reset_ts": reset_ts})
    if not windows:
        return None
    return {"windows": windows, "captured_at": time.time()}


def _cache_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "cache" / _CACHE_NAME


def _read_cache() -> dict[str, Any]:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(data: dict[str, Any]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".hdr-usage-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def record(token: str, headers: Optional[Mapping[str, str]]) -> Optional[dict[str, Any]]:
    """Merge a response's quota headers into the cache entry for *token*.

    Claims missing from this response (``7d_oi`` on a non-Fable reply) keep
    their previous value, so a Haiku title call never erases the Fable bar.
    """
    parsed = parse_unified_headers(headers)
    if not token or parsed is None:
        return None
    key = token_fingerprint(token)
    with _lock:
        data = _read_cache()
        previous = {w["claim"]: w for w in (data.get(key) or {}).get("windows", []) if isinstance(w, dict)}
        for window in parsed["windows"]:
            previous[window["claim"]] = window
        order = [claim for claim, _ in _CLAIMS]
        merged = {
            "windows": sorted(previous.values(), key=lambda w: order.index(w["claim"]) if w["claim"] in order else 99),
            "captured_at": parsed["captured_at"],
        }
        data[key] = merged
        try:
            _write_cache(data)
        except OSError:
            logger.debug("could not persist Anthropic header usage", exc_info=True)
    return merged


def cached(token: str) -> Optional[dict[str, Any]]:
    entry = _read_cache().get(token_fingerprint(token)) if token else None
    return entry if isinstance(entry, dict) and entry.get("windows") else None


def probe(token: str, *, timeout: float = 30.0) -> Optional[dict[str, Any]]:
    """Send a 1-output-token Fable request and record its quota headers.

    Costs a few input tokens of the account's own subscription quota; only the
    Desktop account menu opening asks for it (``probe=1``).
    """
    from agent.anthropic_adapter import _COMMON_BETAS, _OAUTH_ONLY_BETAS, _get_claude_code_version

    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": ",".join([*_COMMON_BETAS, *_OAUTH_ONLY_BETAS]),
        "user-agent": f"claude-cli/{_get_claude_code_version()} (external, cli)",
        "x-app": "cli",
        "content-type": "application/json",
    }
    body = {
        "model": PROBE_MODEL,
        "max_tokens": 1,
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
        "messages": [{"role": "user", "content": "."}],
    }
    with httpx.Client(timeout=timeout) as client:
        response = client.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
    # 429 responses carry the headers too (that is how an exhausted window reads 100%).
    return record(token, response.headers)


def probe_due(token: str) -> bool:
    entry = cached(token)
    return entry is None or time.time() - float(entry.get("captured_at") or 0) >= PROBE_MIN_INTERVAL_SECONDS


def to_windows(entry: dict[str, Any]):
    from agent.account_usage import AccountUsageWindow

    out = []
    for window in entry.get("windows") or []:
        reset_ts = window.get("reset_ts")
        reset_at = datetime.fromtimestamp(reset_ts, tz=UTC) if reset_ts else None
        # A window whose reset already passed has rolled over to 0%.
        used = 0.0 if reset_at and reset_at <= datetime.now(UTC) else float(window["used_percent"])
        scope = "model" if window.get("claim") == "7d_oi" else "account"
        out.append(AccountUsageWindow(label=window["label"], used_percent=used, reset_at=reset_at, scope=scope))
    return out
