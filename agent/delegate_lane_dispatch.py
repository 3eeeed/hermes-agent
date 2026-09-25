"""Delegate mode's ``delegate_task`` seam: a spawn issued during a Delegate turn runs on its lane.

Kept apart from ``agent.delegate_orchestrator`` (turn policy) so ``run_agent`` grows one call only.
"""
from __future__ import annotations

from typing import Any


def dispatch_lane_task(agent: Any, function_args: dict) -> str | None:
    """Run a Delegate-mode spawn on its lane, synchronously; None outside Delegate mode.

    The orchestrator waits for the worker in-turn so review and correction happen before the
    user hears back."""
    from agent.delegate_orchestrator import after_dispatch, before_dispatch, routing_for_tasks
    from tools.delegate_tool import _strip_model_hidden_task_fields, delegate_task
    from tools.registry import tool_error
    tasks = function_args.get("tasks")
    lane_route, lane_error = routing_for_tasks(agent, tasks if tasks is not None else [function_args])
    if lane_error:
        return tool_error(lane_error)
    if lane_route is None or (function_args.get("action") or "spawn") != "spawn":
        return None
    if (refusal := before_dispatch(agent)) is not None:
        return tool_error(refusal)
    return after_dispatch(agent, delegate_task(
        goal=function_args.get("goal"), context=function_args.get("context"),
        tasks=_strip_model_hidden_task_fields(tasks), background=False,
        images=function_args.get("images"), parent_agent=agent, credentials_cfg=lane_route,
    ))
