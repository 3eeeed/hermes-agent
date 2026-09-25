"""Pre-bound chat threads -> existing sessions (``<hermes_home>/thread_session_bindings.json``).

Used to surface old (e.g. Desktop) sessions as Discord forum posts: the first message in a bound
thread switches that thread's session key onto the stored session, so the conversation continues
with its full history. Applied only while the thread's current session is still empty, so ``/new``
inside the thread keeps working afterwards.

    {"discord": {"<thread id>": "<session id>"}}
"""

import asyncio
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _bindings_path():
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "thread_session_bindings.json"


def bound_session_for(platform: str, thread_id: Optional[str]) -> Optional[str]:
    if not thread_id:
        return None
    try:
        data = json.loads(_bindings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = (data.get(platform) or {}).get(str(thread_id)) if isinstance(data, dict) else None
    return str(value) if value else None


def _resolve_target(db: Any, current_id: str, target: str) -> Optional[str]:
    """Target (walked to its compression tip) if the thread's current session is still empty."""
    current = db.get_session(current_id) or {}
    if int(current.get("message_count") or 0) > 0:
        return None  # the thread already has its own conversation (e.g. after /new)
    if not db.get_session(target):
        logger.warning("thread binding target session %s not found", target)
        return None
    return db.get_compression_tip(target) or target


async def apply_thread_session_binding(runner: Any, source: Any, session_entry: Any, session_key: str):
    """Return the (possibly switched) session entry for a pre-bound thread."""
    platform = getattr(getattr(source, "platform", None), "value", "")
    target = bound_session_for(platform, getattr(source, "thread_id", None) or getattr(source, "chat_id", None))
    if not target or session_entry.session_id == target:
        return session_entry
    db = getattr(getattr(runner, "_session_db", None), "_db", None) or getattr(runner, "_session_db", None)
    if db is None:
        return session_entry
    try:
        resolved = await asyncio.to_thread(_resolve_target, db, session_entry.session_id, target)
        if not resolved or resolved == session_entry.session_id:
            return session_entry
        switched = await runner.async_session_store.switch_session(session_key, resolved)
    except Exception:
        logger.warning("thread session binding failed for %s", session_key, exc_info=True)
        return session_entry
    if switched is None:
        return session_entry
    runner._evict_cached_agent(session_key)
    logger.info("Bound %s thread %s to existing session %s", platform, source.thread_id, resolved)
    return switched
