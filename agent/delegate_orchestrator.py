"""Delegate mode: the session's own model leads, briefs lane workers, reviews, and verifies.

The orchestrator is the live session agent with its full history and persona. For the
duration of a Delegate turn its workspace-writing tools are refused (the implementation
belongs to a worker), and every ``delegate_task`` it issues is routed to a configured
lane — provider, model and reasoning effort — and runs synchronously so the review and
any correction happen inside the same turn.

Review is enforced, not only requested: each worker result carries the files that
actually changed during that attempt (a git snapshot taken before the worker started),
the orchestrator may not answer until it has run at least one check after the last
worker finished, and the number of worker attempts per turn is bounded.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Any, Iterator

# Tools that change the workspace. Reading, searching, and running checks stay the
# orchestrator's own work; everything that writes goes through a worker.
ORCHESTRATOR_BLOCKED_TOOLS = frozenset({"write_file", "patch", "execute_code"})

# A first attempt plus two corrections.
MAX_WORKER_ATTEMPTS = 3
_CHECK_TOOLS = frozenset({"terminal"})
_MAX_LISTED_PATHS = 40
_GIT_TIMEOUT_S = 15
# Build/cache output the worker's checks produce; listing it would bury the real changes.
_NOISE_PARTS = ("__pycache__/", ".pytest_cache/", "node_modules/", ".mypy_cache/", ".ruff_cache/")


@dataclass
class _TurnState:
    attempts: int = 0
    awaiting_check: bool = False
    verify_nudged: bool = False
    workspace: str = ""
    baseline: dict[str, str] | None = None
    changed: list[str] = field(default_factory=list)
    lane: str = ""
    worker_running: bool = False
    tracked: bool | None = None  # False once a review found no git repo to diff against
    interrupted: bool = False
    on_phase: Callable[[dict], None] | None = None
    # Live-status facts for a mid-turn "what are you doing?" (agent.delegate_status).
    phase: str = "planning"
    goal: str = ""
    model: str = ""
    owner_session_id: str = ""
    worker_started: float = 0.0


def _publish(state: _TurnState, phase: str) -> None:
    """Tell the UI where the turn stands; a display failure never touches the turn."""
    state.phase = phase
    if state.on_phase is None:
        return
    try:
        state.on_phase({"phase": phase, "attempt": state.attempts, "max_attempts": MAX_WORKER_ATTEMPTS,
                        "lane": state.lane,
                        # None = unknown (no git repo); never report an unmeasured 0.
                        "changed_count": None if state.tracked is False else len(state.changed)})
    except Exception:
        pass


_lock = threading.Lock()
_turns: dict[str, _TurnState] = {}


def blocked_message(tool_name: str, task_id: str) -> str | None:
    """Pre-tool policy: refuse writes for the orchestrator; note the checks it runs."""
    if not task_id:
        return None
    with _lock:
        state = _turns.get(task_id)
        if state is None:
            return None
        if tool_name in _CHECK_TOOLS:
            state.awaiting_check = False
            return None
    if tool_name not in ORCHESTRATOR_BLOCKED_TOOLS:
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
        if worker.get("credential_id"):
            route["credential_id"] = worker["credential_id"]
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
    route = routes[name]
    if route.get("credential_id") and not _account_registered(route["provider"], route["credential_id"]):
        return None, (f"Delegate mode: the account chosen for lane '{name}' is no longer registered for "
                      f"{route['provider']}. Tell the user to pick another account in Delegate settings.")
    agent._delegate_active_lane = name
    return route, None


def _account_registered(provider: str, credential_id: str) -> bool:
    try:
        from agent.credential_pool import load_pool
        return any(entry.id == credential_id for entry in load_pool(provider).entries())
    except Exception:
        return False


# ── workspace evidence ──────────────────────────────────────────────────────

def _git(workspace: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", workspace, *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=_GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _digest(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            return hashlib.sha1(handle.read()).hexdigest()
    except OSError:
        return "missing"


def snapshot(workspace: str) -> dict[str, str] | None:
    """Status + content digest of every dirty/untracked path; None outside a git repo."""
    status = _git(workspace, "status", "--porcelain=v1", "-uall", "-z")
    if status is None:
        return None
    state: dict[str, str] = {}
    for entry in filter(None, status.split("\0")):
        code, path = entry[:2], entry[3:]
        if any(part in f"/{path}" for part in _NOISE_PARTS) or path.endswith((".pyc", ".DS_Store")):
            continue
        state[path] = f"{code}:{_digest(os.path.join(workspace, path))}"
    return state


def changed_since(workspace: str, before: dict[str, str] | None) -> list[str] | None:
    after = snapshot(workspace)
    if before is None or after is None:
        return None
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


def _workspace_for(task_id: str) -> str:
    try:
        from tools.terminal_tool import get_session_cwd
        recorded = get_session_cwd(task_id)
    except Exception:
        recorded = None
    if recorded and os.path.isdir(recorded):
        return recorded
    try:
        from tools.terminal_tool_config import _tenv
        configured = _tenv("TERMINAL_CWD")
    except Exception:
        configured = os.environ.get("TERMINAL_CWD", "")
    return configured if configured and os.path.isdir(configured) else os.getcwd()


def before_dispatch(agent: Any, goal: str = "", model: str = "") -> str | None:
    """Admit one worker attempt and capture its baseline; an error string refuses it."""
    task_id = getattr(agent, "_delegate_task_id", "")
    with _lock:
        state = _turns.get(task_id)
        if state is None:
            return None
        if state.attempts >= MAX_WORKER_ATTEMPTS:
            return (f"Delegate mode: the worker budget for this message is spent ({MAX_WORKER_ATTEMPTS} "
                    "attempts). Stop and tell the user exactly what is done, what failed, and what is left.")
        state.attempts += 1
        state.lane = getattr(agent, "_delegate_active_lane", "") or getattr(agent, "_delegate_default_lane", "") or ""
        workspace = state.workspace = _workspace_for(task_id)
    baseline = snapshot(workspace)
    with _lock:
        state.baseline = baseline
        state.worker_running = True
        state.goal, state.model = goal or "", model or ""
        state.owner_session_id = str(getattr(agent, "session_id", "") or "")
        state.worker_started = time.time()
    _publish(state, "worker")
    return None


def after_dispatch(agent: Any, result: str) -> str:
    """Attach the observed change list to the worker result and require a fresh check."""
    task_id = getattr(agent, "_delegate_task_id", "")
    with _lock:
        state = _turns.get(task_id)
        if state is None:
            return result
        workspace, baseline, attempt = state.workspace, state.baseline, state.attempts
    changed = changed_since(workspace, baseline)
    with _lock:
        state.awaiting_check = True
        state.verify_nudged = False
        state.worker_running = False
        state.tracked = changed is not None
        if changed:
            state.changed = sorted(set(state.changed) | set(changed))
    _publish(state, "review")
    if changed is None:
        observed = {"available": False, "note": "workspace is not a git repository; review by reading files."}
    else:
        observed = {"available": True, "changed_paths": changed[:_MAX_LISTED_PATHS],
                    "changed_count": len(changed)}
        if not changed:
            observed["note"] = "the worker changed no files; treat its report as unverified."
    evidence = {"workspace": workspace, "attempt": attempt, "attempts_left": MAX_WORKER_ATTEMPTS - attempt,
                "observed_changes": observed,
                "next": "The report above is a claim. Read these files, run the success commands and "
                        "boundary cases yourself, then either answer or send one correction."}
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return f"{result}\n\n[delegate review evidence]\n{json.dumps(evidence, ensure_ascii=False)}"
    if isinstance(payload, dict):
        payload["delegate_review"] = evidence
        return json.dumps(payload, ensure_ascii=False)
    return result


def verify_nudge(agent: Any) -> str | None:
    """Stop gate: the orchestrator may not answer before checking the latest worker's result."""
    task_id = getattr(agent, "_delegate_task_id", "")
    with _lock:
        state = _turns.get(task_id)
        if state is None or not state.awaiting_check or state.verify_nudged:
            return None
        state.verify_nudged = True
        changed = list(state.changed[:_MAX_LISTED_PATHS])
    listed = ", ".join(changed) if changed else "(none observed)"
    return ("[Delegate review] You have not run any check since the last worker finished. Before "
            f"answering: read the changed files ({listed}), run the brief's success command, and try the "
            "boundary inputs the requirement implies (malformed or alternative formats, empty values, edge "
            "values, old data) against the delivered code. If anything fails, send one correction with the "
            "evidence; otherwise answer with what you ran and what it printed.")


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
        "Each worker result carries `delegate_review.observed_changes` (the files that really changed "
        "during that attempt). The report is a claim: read those files, run the success commands, and "
        "probe boundary inputs the requirement implies. If something is wrong or missing, send a "
        f"correction naming the specific gap and the evidence. At most {MAX_WORKER_ATTEMPTS} worker "
        "attempts per message. Do not commit, push, reset, stash or discard the worker's changes unless "
        "the user asked.",
        "Tell the user what changed, what you ran and what it output; never call something verified "
        "unless you ran the check and saw it pass.",
        "Keep the user informed while you work, in their language: right before each delegate_task "
        "call write one short line saying what you are handing off and to which lane and model; when "
        "a worker returns, write one short line saying what came back and what you will check next.",
        "If the user writes to you mid-turn, reply to that message first in a short visible answer "
        "(what you are doing, what is left, or whatever they asked), then continue the work. Never "
        "ignore it or wait for the end of the turn to acknowledge it.",
        "Lanes:",
        *lanes,
    ])


@contextlib.contextmanager
def orchestrator_turn(
    agent: Any, task_id: str, options: dict, on_phase: Callable[[dict], None] | None = None,
) -> Iterator[_TurnState]:
    """Arm the orchestrator gate, lane routing, review state and orchestrator effort for one turn.

    Yields the turn state; the caller sets ``interrupted`` from the turn result (the agent's own
    interrupt flag is already cleared by the finalizer when control returns here)."""
    from hermes_constants import parse_reasoning_effort
    orchestrator = options.get("orchestrator") or {}
    effort = parse_reasoning_effort(orchestrator.get("effort")) if orchestrator.get("effort") else None
    saved_reasoning = getattr(agent, "reasoning_config", None)
    with _lock:
        state = _turns[task_id] = _TurnState(workspace=_workspace_for(task_id), on_phase=on_phase)
    _publish(state, "planning")
    agent._delegate_task_id = task_id
    agent._delegate_lane_routes = lane_routes(options)
    agent._delegate_default_lane = default_lane(options)
    if effort is not None:
        agent.reasoning_config = effort
    try:
        yield state
    finally:
        with _lock:
            _turns.pop(task_id, None)
        # A worker still marked running here means the turn ended under it (Stop, crash,
        # disconnect): its edits may be partial, so say so instead of silently clearing.
        interrupted = state.worker_running or state.interrupted or bool(getattr(agent, "_interrupt_requested", False))
        _publish(state, "interrupted" if interrupted else "done")
        agent._delegate_task_id = ""
        agent._delegate_lane_routes = None
        agent._delegate_default_lane = None
        if effort is not None:
            agent.reasoning_config = saved_reasoning
