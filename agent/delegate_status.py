"""Instant answer to a mid-turn message while a Delegate worker runs.

The orchestrator waits for its worker inside one ``delegate_task`` call, so a steer the user
types meanwhile only reaches the model when the worker returns. The user still gets a reply
at once: a status built from the turn's own facts (phase, lane, model, brief, elapsed time,
the worker's tool activity). The model answers the message itself when the worker returns.
"""
from __future__ import annotations

import time
from typing import Any

_GOAL_CHARS = 220


def _worker_activity(owner_session_id: str) -> dict[str, Any]:
    """Tool count / last tool of the running worker this orchestrator owns ({} when unknown)."""
    if not owner_session_id:
        return {}
    from tools.delegate_tool_registry import list_active_subagents
    for record in list_active_subagents():
        if record.get("owner_agent_session_id") == owner_session_id and record.get("status") == "running":
            return {"tool_count": record.get("tool_count") or 0, "last_tool": record.get("last_tool") or ""}
    return {}


def live_status(task_id: str) -> dict[str, Any] | None:
    """Snapshot of a running Delegate turn's worker; None when no worker is running."""
    from agent.delegate_orchestrator import MAX_WORKER_ATTEMPTS, _lock, _turns
    with _lock:
        state = _turns.get(task_id) if task_id else None
        if state is None or not state.worker_running:
            return None
        snap = {"phase": state.phase, "lane": state.lane, "model": state.model, "goal": state.goal,
                "attempt": state.attempts, "max_attempts": MAX_WORKER_ATTEMPTS,
                "elapsed_s": max(0, int(time.time() - state.worker_started)) if state.worker_started else 0,
                "owner_session_id": state.owner_session_id}
    snap.update(_worker_activity(snap.pop("owner_session_id")))
    return snap


def _elapsed(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    return f"{minutes} دقيقة و{secs} ثانية" if minutes else f"{secs} ثانية"


def status_reply(task_id: str) -> str | None:
    """User-facing status line for a running worker; None when nothing is running."""
    snap = live_status(task_id)
    if snap is None:
        return None
    goal = " ".join(snap["goal"].split())
    if len(goal) > _GOAL_CHARS:
        goal = goal[:_GOAL_CHARS].rstrip() + "…"
    who = f"`{snap['lane']}`" + (f" ({snap['model']})" if snap["model"] else "")
    lines = [f"وصلتني رسالتك. دلوقتي العامل {who} شغال على المهمة "
             f"(محاولة {snap['attempt']} من {snap['max_attempts']}) بقاله {_elapsed(snap['elapsed_s'])}."]
    if goal:
        lines.append(f"المهمة: {goal}")
    if snap.get("tool_count"):
        last = f"، وآخر أداة استخدمها `{snap['last_tool']}`" if snap.get("last_tool") else ""
        lines.append(f"استخدم لحد دلوقتي {snap['tool_count']} أداة{last}.")
    lines.append("أول ما يخلص هقرا رسالتك وأرد عليك بالتفصيل قبل ما أكمل المراجعة.")
    return "\n".join(lines)
