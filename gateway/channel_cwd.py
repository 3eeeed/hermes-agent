"""Per-channel working directory for messaging sessions (``<platform>.channel_cwds``).

A chat surface (Discord forum, Slack channel) can be bound to a project folder so every session
opened there starts in that folder: tools, context files (AGENTS.md) and the persisted session
``cwd`` (which Desktop uses to group sessions under Projects) all agree.

    discord:
      channel_cwds:
        "1234567890": /srv/workspaces/wispers     # forum or channel id -> folder

Exact thread/channel id wins, then the parent channel/forum (threads and forum posts inherit).
A missing folder is ignored with a warning, never guessed.
"""

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


def resolve_channel_cwd(config_extra: Any, *ids: Optional[str]) -> Optional[str]:
    mapping = (config_extra or {}).get("channel_cwds") if isinstance(config_extra, dict) else None
    if not isinstance(mapping, dict) or not mapping:
        return None
    for key in ids:
        raw = mapping.get(str(key)) if key else None
        if raw is None or not str(raw).strip():
            continue
        path = os.path.abspath(os.path.expanduser(str(raw).strip()))
        if os.path.isdir(path):
            return path
        logger.warning("channel_cwds[%s] is not a directory: %s", key, raw)
        return None
    return None


def channel_cwd_for_source(adapters: Any, source: Any) -> Optional[str]:
    """Bound folder for a SessionSource, looked up in its platform adapter's ``config.extra``."""
    adapter = (adapters or {}).get(getattr(source, "platform", None))
    extra = getattr(getattr(adapter, "config", None), "extra", None)
    return resolve_channel_cwd(
        extra, getattr(source, "thread_id", None), getattr(source, "chat_id", None),
        getattr(source, "parent_chat_id", None))


def channel_cwd_for_runner(runner: Any, source: Any) -> Optional[str]:
    """Bound folder for a source, read from the adapter that answers it (multiplex-safe)."""
    try:
        adapter = runner._delivery_adapter_for(source) if hasattr(runner, "_delivery_adapter_for") else None
        adapter = adapter or (getattr(runner, "adapters", None) or {}).get(source.platform)
        extra = getattr(getattr(adapter, "config", None), "extra", None)
        return resolve_channel_cwd(extra, source.thread_id, source.chat_id, getattr(source, "parent_chat_id", None))
    except Exception:
        logger.debug("channel cwd lookup failed", exc_info=True)
        return None


def bind_session_to_channel_cwd(cwd: str, session_key: str, session_id: str, session_db: Any) -> None:
    """Seed the terminal/file tools' cwd for this session (only when the session has not ``cd``'d
    yet) and persist the row's ``cwd`` once so Desktop files the session under its Project."""
    from tools.terminal_tool import get_session_cwd, register_task_env_overrides

    for key in {k for k in (session_key, session_id) if k}:
        if get_session_cwd(key) is None:
            register_task_env_overrides(key, {"cwd": cwd})
    db = getattr(session_db, "_db", session_db)
    if db is None or not session_id:
        return
    try:
        row = db.get_session(session_id) or {}
        if not (row.get("cwd") or "").strip():
            db.update_session_cwd(session_id, cwd, git_repo_root=_git_root(cwd))
    except Exception:
        logger.debug("persisting channel cwd failed", exc_info=True)


def _git_root(cwd: str) -> Optional[str]:
    probe = cwd
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
