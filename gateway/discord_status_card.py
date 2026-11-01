"""Live status card for Discord threads/forum posts (``discord.status_card: true``).

After every turn the bot keeps ONE pinned message per thread up to date — the chat equivalent of
the Desktop status bar: model · reasoning · fast, the account serving the session with its quota
windows and reset times, context occupancy, folder. Runs after delivery, off the reply path;
any failure is logged and ignored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone, UTC
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_MARK = "\u2063"  # invisible separator, marks our own card
_USAGE_TTL = 90.0
_usage_cache: dict[str, tuple] = {}
_card_ids: dict[str, str] = {}


def _state_path():
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "discord_status_cards.json"


def _load_ids() -> None:
    if _card_ids:
        return
    try:
        _card_ids.update(json.loads(_state_path().read_text()))
    except (OSError, ValueError):
        pass


def _save_ids() -> None:
    p = _state_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(_card_ids))
    tmp.replace(p)


def _bar(pct: Optional[float]) -> str:
    if pct is None:
        return ""
    left = max(0, min(100, round(100 - pct)))
    full = round(left / 10)
    return "▰" * full + "▱" * (10 - full) + f" {left}%"


def _until(reset_at: Optional[datetime]) -> str:
    if not reset_at:
        return ""
    secs = (reset_at - datetime.now(UTC)).total_seconds()
    if secs <= 0:
        return "الآن"
    h, m = divmod(int(secs // 60), 60)
    d, h = divmod(h, 24)
    return f"{d}ي {h}س" if d else (f"{h}س {m}د" if h else f"{m}د")


def _active_entry(agent: Any):
    pool = getattr(agent, "_credential_pool", None)
    if pool is None:
        return None, 0, 0
    try:
        entries = list(pool.entries())
        cur = pool.current() or pool.peek()
    except Exception:
        logger.debug("status card: credential pool unreadable", exc_info=True)
        return None, 0, 0
    idx = next((i for i, e in enumerate(entries, 1) if cur is not None and e.id == cur.id), 0)
    return cur, idx, len(entries)


def _usage_for(provider: str, entry: Any, agent: Any):
    from agent.account_usage import fetch_account_usage
    key = f"{provider}:{getattr(entry, 'id', '') or 'default'}"
    hit = _usage_cache.get(key)
    if hit and time.monotonic() - hit[0] < _USAGE_TTL:
        return hit[1]
    try:
        if entry is not None:
            snap = fetch_account_usage(provider, base_url=entry.runtime_base_url, api_key=entry.runtime_api_key)
        else:
            snap = fetch_account_usage(provider, base_url=getattr(agent, "base_url", None), api_key=getattr(agent, "api_key", None))
    except Exception:
        logger.debug("status card: usage fetch failed for %s", provider, exc_info=True)
        snap = None
    _usage_cache[key] = (time.monotonic(), snap)
    return snap


def render_card(agent: Any, agent_result: dict[str, Any], cwd: str) -> str:
    """Build the card text from the live agent (sync; may do one cached HTTP usage call)."""
    model = (agent_result.get("model") or getattr(agent, "model", "") or "?").rsplit("/", 1)[-1]
    provider = str(getattr(agent, "provider", "") or agent_result.get("provider") or "")
    rc = getattr(agent, "reasoning_config", None) or {}
    effort = (rc.get("effort") if isinstance(rc, dict) else None) or ("off" if isinstance(rc, dict) and rc.get("enabled") is False else "default")
    fast = "⚡ fast" if getattr(agent, "service_tier", None) in ("priority", "fast") else ""
    ctx_used = agent_result.get("last_prompt_tokens") or 0
    ctx_len = agent_result.get("context_length") or 0
    ctx = f"{round(ctx_used / ctx_len * 100)}% ({ctx_used // 1000}k/{ctx_len // 1000}k)" if ctx_len else "—"

    entry, idx, total = _active_entry(agent)
    acct = ""
    if entry is not None:
        label = getattr(entry, "label", "") or str(idx)
        acct = f"👤 أكونت **{label}**" + (f" ({idx}/{total})" if total > 1 else "")
    snap = _usage_for(provider, entry, agent) if provider else None

    lines = [f"🧠 **{model}** · تفكير `{effort}`" + (f" · {fast}" if fast else "") + f" · `{provider or '-'}`"]
    if acct:
        lines.append(acct)
    if snap is not None and getattr(snap, "windows", None):
        for w in snap.windows:
            reset = _until(w.reset_at)
            lines.append(f"⏳ {w.label}: {_bar(w.used_percent)}" + (f" · يتجدد بعد {reset}" if reset else ""))
    elif snap is not None and getattr(snap, "unavailable_reason", None):
        lines.append(f"⏳ الحصة: {snap.unavailable_reason}")
    lines.append(f"📊 السياق: {ctx} · 📁 `{cwd or '-'}`")
    lines.append(f"-# آخر تحديث {datetime.now().strftime('%H:%M')} · `/model` `/reasoning` `/fast` `/usage` {_MARK}")
    return "\n".join(lines)[:1990]


async def update_status_card(adapter: Any, thread_id: str, text: str) -> None:
    """Edit this thread's pinned card, or post + pin a new one."""
    client = getattr(adapter, "_client", None) or getattr(adapter, "client", None)
    if client is None:
        return
    _load_ids()
    try:
        channel = client.get_channel(int(thread_id)) or await client.fetch_channel(int(thread_id))
    except Exception:
        logger.debug("status card: channel %s unavailable", thread_id, exc_info=True)
        return
    msg_id = _card_ids.get(str(thread_id))
    if msg_id:
        try:
            msg = await channel.fetch_message(int(msg_id))
            if msg.content != text:
                await msg.edit(content=text)
            return
        except Exception:
            logger.debug("status card: stale card %s in %s, reposting", msg_id, thread_id, exc_info=True)
    try:
        import discord  # type: ignore
        msg = await channel.send(text, allowed_mentions=discord.AllowedMentions.none(), silent=True)
        try:
            await msg.pin(reason="Hermes status card")
            # Drop Discord's "X pinned a message" system notice so the post stays clean.
            async for notice in channel.history(limit=3, after=msg):
                if getattr(notice.type, "name", "") == "pins_add" and notice.author.id == client.user.id:
                    await notice.delete()
        except Exception:
            logger.debug("status card: pin failed in %s", thread_id, exc_info=True)
        _card_ids[str(thread_id)] = str(msg.id)
        _save_ids()
    except Exception:
        logger.debug("status card: post failed in %s", thread_id, exc_info=True)


def schedule_status_card(runner: Any, source: Any, session_key: str, agent_result: dict[str, Any]) -> None:
    """Fire-and-forget from the gateway turn: only Discord threads, only when enabled."""
    try:
        from gateway.config import Platform
        if getattr(source, "platform", None) != Platform.DISCORD or not getattr(source, "thread_id", None):
            return
        from gateway.run import _load_gateway_config
        if not ((_load_gateway_config().get("discord") or {}).get("status_card")):
            return
        adapter = (getattr(runner, "adapters", None) or {}).get(Platform.DISCORD)
        agent = runner._resident_agent_for(session_key) if hasattr(runner, "_resident_agent_for") else None
        if adapter is None or agent is None:
            return
        from gateway.channel_cwd import channel_cwd_for_runner
        cwd = channel_cwd_for_runner(runner, source) or ""
        thread_id = str(source.thread_id)

        async def _run():
            try:
                text = await asyncio.to_thread(render_card, agent, dict(agent_result), cwd or "")
                await update_status_card(adapter, thread_id, text)
            except Exception:
                logger.debug("status card update failed", exc_info=True)

        asyncio.get_running_loop().create_task(_run())
    except Exception:
        logger.debug("status card scheduling failed", exc_info=True)
