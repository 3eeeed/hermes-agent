"""Delegate mode: the session's own model leads, briefs lane workers, reviews, and verifies.

The orchestrator is the live session agent with its full history and persona. For the
duration of a Delegate turn its workspace-writing tools are refused (the implementation
belongs to a worker), and every ``delegate_task`` it issues is routed to a configured
lane — provider, model and reasoning effort — and runs synchronously so the review and
any correction happen inside the same turn.
"""
from __future__ import annotations

import contextlib
import threading
from typing import Any, Iterator

# Tools that change the workspace. Reading, searching, and running checks stay the
# orchestrator's own work; everything that writes goes through a worker.
ORCHESTRATOR_BLOCKED_TOOLS = frozenset({"write_file", "patch", "execute_code"})

_lock = threading.Lock()
_active_task_ids: set[str] = set()


def blocked_message(tool_name: str, task_id: str) -> str | None:
    if tool_name not in ORCHESTRATOR_BLOCKED_TOOLS or not task_id:
        return None
    with _lock:
        if task_id not in _active_task_ids:
            return None
    return (f"Delegate mode: `{tool_name}` is reserved for workers. Brief a worker with "
            "delegate_task (pick a lane) to make this change, then review and verify it.")


def lane_routes(options: dict) -> dict[str, dict[str, Any]]:
    """Lane name → the per-call routing config delegate_task already understands."""
    routes = {}
    for worker in options["workers"]:
        route = {"provider": worker["provider"], "model": worker["model"], "fallback_providers": []}
        if worker.get("effort"):
            route["reasoning_effort"] = worker["effort"]
        routes[worker["lane"]] = route
    return routes


def default_lane(options: dict) -> str:
    return options.get("default_lane") or options["workers"][0]["lane"]


def routing_for_tasks(agent: Any, tasks: Any) -> tuple[dict | None, str | None]:
    """Resolve the one lane a delegate_task call targets; ``(None, None)`` outside Delegate."""
    routes = getattr(agent, "_delegate_lane_routes", None)
    if not isinstance(routes, dict) or not routes:
        return None, None
    fallback = getattr(agent, "_delegate_default_lane", None)
    names = {(t.get("lane") or fallback) for t in tasks if isinstance(t, dict)} if isinstance(tasks, list) else {fallback}
    if len(names) != 1:
        return None, "Delegate mode: one delegate_task call targets exactly one lane; split the tasks per lane."
    name = names.pop()
    if name not in routes:
        return None, f"Delegate mode: unknown lane '{name}'. Configured lanes: {', '.join(routes)}."
    return routes[name], None


def dispatch_lane_task(agent: Any, function_args: dict) -> str | None:
    """Run a Delegate-mode ``delegate_task`` spawn on its lane, synchronously; None outside Delegate.

    The orchestrator waits for the worker in-turn so review and correction happen before the
    user hears back."""
    from tools.delegate_tool import _strip_model_hidden_task_fields, delegate_task
    from tools.registry import tool_error
    tasks = function_args.get("tasks")
    lane_route, lane_error = routing_for_tasks(agent, tasks if tasks is not None else [function_args])
    if lane_error:
        return tool_error(lane_error)
    if lane_route is None or (function_args.get("action") or "spawn") != "spawn":
        return None
    return delegate_task(
        goal=function_args.get("goal"), context=function_args.get("context"),
        tasks=_strip_model_hidden_task_fields(tasks), background=False,
        images=function_args.get("images"), parent_agent=agent, credentials_cfg=lane_route,
    )


def turn_note(options: dict) -> str:
    lanes = []
    for worker in options["workers"]:
        effort = worker.get("effort") or "default"
        duty = f" — {worker['responsibility']}" if worker.get("responsibility") else ""
        lanes.append(f"- `{worker['lane']}`: {worker['provider']}/{worker['model']}, effort {effort}{duty}")
    return "\n".join([
        "[Delegate mode is on for this message. You lead; workers implement.]",
        "Understand the request, read the workspace, and plan. Then hand the change to a worker with "
        "delegate_task — pass `lane` on each task (omit it for the default lane "
        f"`{default_lane(options)}`). The worker does not see this conversation: write each goal so it "
        "can finish alone — the goal, the files and behavior in scope, the constraints, what must not "
        "change, and the exact commands that decide success.",
        "write_file, patch and execute_code are refused for you in this mode. Reading, searching and "
        "running checks with terminal stay yours. Do not paste implementation code in your reply.",
        "A worker's report is a claim: read the changed files (git diff) and run the success commands "
        "yourself. If something is wrong or missing, send a correction naming the specific gap and the "
        "evidence, at most two corrections, then report what is left. Do not commit, push, reset, stash "
        "or discard the worker's changes unless the user asked.",
        "Tell the user what changed, what you ran and what it output; never call something verified "
        "unless you ran the check and saw it pass.",
        "Lanes:",
        *lanes,
    ])


@contextlib.contextmanager
def orchestrator_turn(agent: Any, task_id: str, options: dict) -> Iterator[None]:
    """Arm the orchestrator gate, lane routing and orchestrator effort for one turn."""
    from hermes_constants import parse_reasoning_effort
    orchestrator = options.get("orchestrator") or {}
    effort = parse_reasoning_effort(orchestrator.get("effort")) if orchestrator.get("effort") else None
    saved_reasoning = getattr(agent, "reasoning_config", None)
    with _lock:
        _active_task_ids.add(task_id)
    agent._delegate_lane_routes = lane_routes(options)
    agent._delegate_default_lane = default_lane(options)
    if effort is not None:
        agent.reasoning_config = effort
    try:
        yield
    finally:
        with _lock:
            _active_task_ids.discard(task_id)
        agent._delegate_lane_routes = None
        agent._delegate_default_lane = None
        if effort is not None:
            agent.reasoning_config = saved_reasoning
