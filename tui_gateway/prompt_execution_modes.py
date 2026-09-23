"""Bounded native-mode input contract; unsupported execution never falls back to Normal.

This is deliberately an admission guard, not a simulated multi-agent runner. A mode
is not advertised as executable until its child lifecycle, routing, and persistence
can be honored end to end.
"""
from __future__ import annotations

from typing import Literal
from dataclasses import dataclass
import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class _Options(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Route(_Options):
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    effort: Literal["none", "low", "medium", "high", "xhigh"] | None = None


class Worker(Route):
    lane: str = Field(min_length=1, max_length=80)
    toolsets: list[Literal["safe", "search", "web"]] | None = Field(default=None, max_length=3)


class DelegateOptions(_Options):
    orchestrator: Route | None = None
    workers: list[Worker] = Field(min_length=1, max_length=4)
    max_iterations: int = Field(ge=1, le=20)
    max_concurrency: int = Field(default=1, ge=1, le=4)
    permissions: Literal["read-only"] = "read-only"


class DebateOptions(_Options):
    side_a: Route
    side_b: Route
    judge: Route
    rounds: int = Field(ge=1, le=3)
    permissions: Literal["read-only"] = "read-only"


@dataclass(frozen=True)
class ExecutionSpec:
    mode: str
    _options_json: str

    @property
    def options(self) -> dict:
        # A fresh copy on every read prevents mutation across attempts or queue slots.
        return json.loads(self._options_json)

    def as_wire(self) -> dict:
        return {"mode": self.mode, "options": self.options}


def build_execution_spec(mode: str | None, options: dict | None) -> ExecutionSpec:
    selected = mode or "normal"
    if selected == "normal":
        if options:
            raise ValueError("Normal mode does not accept mode_options")
        return ExecutionSpec("normal", "{}")
    model = {"delegate": DelegateOptions, "debate": DebateOptions}.get(selected)
    if model is None:
        raise ValueError(f"Unknown execution mode: {selected}")
    try:
        parsed = model.model_validate(options, strict=True)
    except ValidationError as exc:
        raise ValueError(f"Invalid {selected} mode options: {exc.errors(include_url=False, include_input=False)}") from exc
    canonical = json.dumps(parsed.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return ExecutionSpec(selected, canonical)


def require_execution_spec(spec: ExecutionSpec | dict | None, *, expected_mode: str) -> ExecutionSpec:
    if spec is None:
        raise ValueError(f"execution spec lost for {expected_mode} turn")
    if isinstance(spec, dict):
        if set(spec) != {"mode", "options"}:
            raise ValueError("execution spec lost or malformed")
        spec = build_execution_spec(spec["mode"], spec["options"])
    if not isinstance(spec, ExecutionSpec) or spec.mode != expected_mode:
        raise ValueError(f"execution spec lost for {expected_mode} turn")
    return spec


def admission_error(mode: str | None, options: dict | None) -> str | None:
    """Validate before any history/queue/attachment mutation. Only Normal runs today."""
    selected = mode or "normal"
    if selected == "normal":
        return "Normal mode does not accept mode_options" if options else None
    model = {"delegate": DelegateOptions, "debate": DebateOptions}.get(selected)
    if model is None:
        return f"Unknown execution mode: {selected}"
    try:
        model.model_validate(options, strict=True)
    except ValidationError as exc:
        return f"Invalid {selected} mode options: {exc.errors(include_url=False, include_input=False)}"
    if selected == "debate":
        return None  # Per-session isolation/text gates still run before any mutation.
    return f"{selected.capitalize()} mode is unavailable: native child orchestration is not installed; no turn was started"


def stage_execution_spec(session: dict, spec: ExecutionSpec | None) -> None:
    """Pin (or clear) the mode the next turn runs under. Caller holds ``history_lock``."""
    if spec is None or spec.mode == "normal":
        session.pop("_execution_spec", None)
        session.pop("_execution_mode", None)
    else:
        session["_execution_spec"] = spec
        session["_execution_mode"] = spec.mode


def submit_gate_error(spec: ExecutionSpec, session: dict, text, turn_isolation: bool) -> str | None:
    """Per-session refusals for an agent-mode send, checked before any history mutation."""
    if spec.mode == "normal":
        return None
    if turn_isolation:
        # Agent modes run on the session's own in-process agent; isolated compute hosts do not.
        return f"{spec.mode.capitalize()} mode is unavailable while dashboard.turn_isolation is on"
    if spec.mode == "debate" and (not isinstance(text, str) or session.get("attached_images")):
        return "Debate mode does not accept image turns"
    return None
