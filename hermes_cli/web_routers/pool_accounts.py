"""Per-account routes for the desktop account menu (pooled Codex / Claude accounts).

Usage per account, the per-session account pin, and removal by stable id. The provider
comes from the URL, so ``_POOL_UI_PROVIDERS`` is the allowlist that keeps these routes
off pools the account menu does not own. Every route runs in the requested profile's
scope: one backend serves many profiles and each has its own ``auth.json``/``state.db``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

from hermes_cli.web_models import CodexSessionCredentialSelect, PooledCredentialRename
from hermes_cli.web_routers._common import config_scoped_to_thread, destructive_profile

_log = logging.getLogger("hermes_cli.web_server")
router = APIRouter()

_POOL_UI_PROVIDERS = ("openai-codex", "anthropic")


def _validated_pool_provider(provider: str) -> str:
    """Normalize a path provider, rejecting any not managed by the account menu."""
    normalized = (provider or "").strip().lower()
    if normalized not in _POOL_UI_PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown account provider")
    return normalized


def _usage_entry(provider: str, entry: Any) -> dict[str, Any]:
    """One account's quota, never its credential material. Fails open per account."""
    from agent.account_usage import fetch_account_usage
    from hermes_cli.web_routers.ops import pool_entry_account_email

    email = pool_entry_account_email(entry)
    try:
        snapshot = fetch_account_usage(provider, base_url=entry.runtime_base_url, api_key=entry.runtime_api_key)
    except Exception:
        _log.debug("%s usage lookup failed for pool entry %s", provider, entry.id, exc_info=True)
        snapshot = None
    if snapshot is None:
        return {"id": entry.id, "available": False, "label": entry.label, "email": email}
    return {
        "available": snapshot.available,
        "details": list(snapshot.details),
        "fetched_at": snapshot.fetched_at.isoformat(),
        "id": entry.id,
        "label": entry.label,
        "email": email,
        "plan": snapshot.plan,
        "unavailable_reason": snapshot.unavailable_reason,
        "windows": [
            {
                "detail": window.detail,
                "label": window.label,
                "reset_at": window.reset_at.isoformat() if window.reset_at else None,
                "used_percent": window.used_percent,
            }
            for window in snapshot.windows
        ],
    }


def _pool_entries(provider: str) -> list[Any]:
    from agent.credential_pool import load_pool

    try:
        return list(load_pool(provider).entries())
    except Exception:
        _log.exception("load_pool(%s) failed while reading usage", provider)
        return []


@router.get("/api/credentials/pool/{provider}/usage")
async def list_credential_pool_usage(provider: str, profile: Optional[str] = None):
    """Per-account quota for a pool-backed provider (Codex, Claude).

    The normal pool endpoint intentionally exposes only operational metadata; the
    account menu needs the limits, so each credential is resolved server-side and only
    the usage response is serialized. One expired account must not hide the others.
    """
    provider = _validated_pool_provider(provider)
    entries = await config_scoped_to_thread(profile, lambda: _pool_entries(provider))
    # One account's slow usage endpoint must not serialize the others behind it.
    usage = await asyncio.gather(*(
        config_scoped_to_thread(profile, lambda entry=entry: _usage_entry(provider, entry)) for entry in entries
    ))
    # Reported even when the pool is empty. Anthropic legitimately starts at
    # zero: Hermes borrows the ambient Claude Code credential, and the pool
    # refuses to adopt it until the user explicitly configures the provider
    # (PR #4210). The desktop menu needs to render its add button in that
    # state, otherwise the first account can never be added from the UI.
    return {"provider": provider, "entries": list(usage), "can_add_accounts": True}


@router.put("/api/credentials/pool/{provider}/session-selection")
async def set_pool_session_credential_selection(provider: str, body: CodexSessionCredentialSelect):
    """Persist a non-secret pool-entry preference (the account pin) for one stored session."""
    from agent.credential_pool import POOL_PIN_KEYS, load_pool
    from hermes_cli.web_server_sessions import _open_session_db_for_profile

    provider = _validated_pool_provider(provider)
    pin_key = POOL_PIN_KEYS[provider]
    session_id = body.session_id.strip()
    credential_id = body.credential_id.strip()
    if not session_id or not credential_id:
        raise HTTPException(status_code=400, detail="session_id and credential_id are required")

    def _run():
        if not any(entry.id == credential_id for entry in load_pool(provider).entries()):
            raise HTTPException(status_code=404, detail="Account was not found")
        db = _open_session_db_for_profile(body.profile, read_only=False)
        try:
            resolved_id = db.resolve_session_id(session_id)
            if not resolved_id or not db.get_session(resolved_id):
                raise HTTPException(status_code=404, detail="Session was not found")
            db.patch_session_model_config(resolved_id, {pin_key: credential_id})
            # Read the persisted value back before confirming: a caller must never paint
            # an account as active if the write was ignored by a stale/missing row.
            saved_credential_id = db.get_session_model_config_value(resolved_id, pin_key)
            if saved_credential_id != credential_id:
                raise HTTPException(status_code=500, detail="Account selection was not persisted")
            return {"ok": True, "session_id": resolved_id, "credential_id": saved_credential_id}
        finally:
            db.close()

    return await config_scoped_to_thread(body.profile, _run)


@router.get("/api/credentials/pool/{provider}/session-selection")
async def get_pool_session_credential_selection(provider: str, session_id: str, profile: Optional[str] = None):
    """Return a session's saved opaque pool id, never credential material."""
    from agent.credential_pool import POOL_PIN_KEYS
    from hermes_cli.web_server_sessions import _open_session_db_for_profile

    provider = _validated_pool_provider(provider)
    pin_key = POOL_PIN_KEYS[provider]
    session_id = session_id.strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    def _run():
        db = _open_session_db_for_profile(profile, read_only=True)
        try:
            resolved_id = db.resolve_session_id(session_id)
            if not resolved_id or not db.get_session(resolved_id):
                raise HTTPException(status_code=404, detail="Session was not found")
            return {"session_id": resolved_id, "credential_id": db.get_session_model_config_value(resolved_id, pin_key)}
        finally:
            db.close()

    return await asyncio.to_thread(_run)


@router.patch("/api/credentials/pool/{provider}/entries/{credential_id}")
async def rename_pool_credential_entry(provider: str, credential_id: str, body: PooledCredentialRename):
    """Relabel an account by its immutable pool id; identity (id, tokens, priority) is untouched."""
    from agent.credential_pool import load_pool

    provider = _validated_pool_provider(provider)
    label = (body.label or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="A label cannot be blank")

    def _run():
        renamed = load_pool(provider).rename_entry(credential_id, label)
        if renamed is None:
            raise HTTPException(status_code=404, detail="Account was not found")
        return {"ok": True, "provider": provider, "id": renamed.id, "label": renamed.label}

    return await config_scoped_to_thread(body.profile, _run)


@router.delete("/api/credentials/pool/{provider}/entries/{credential_id}")
async def remove_pool_credential_entry(provider: str, credential_id: str, profile: Optional[str] = None):
    """Delete an account by its immutable pool id, never a mutable row index."""
    from hermes_cli.web_routers.ops import remove_pool_entry

    provider = _validated_pool_provider(provider)
    return await config_scoped_to_thread(
        destructive_profile(profile, "DELETE /api/credentials/pool/{provider}/entries/{credential_id}"),
        lambda: remove_pool_entry(provider, credential_id))
