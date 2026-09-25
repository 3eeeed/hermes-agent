"""/delegate for messaging chats: the same Delegate turn the Desktop runs, switched per chat.

The lanes come from ``delegate_mode:`` in config.yaml (the same shape the Desktop sends,
validated by ``DelegateOptions``). ``/delegate on`` makes every following message in this chat
a Delegate turn until ``/delegate off``; the switch lives in the conversation's state beside
the /model and /reasoning overrides, so a session boundary (/new, /reset) returns to Normal.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("gateway.run")

_ON = {"on", "enable", "start", "yes"}
_OFF = {"off", "disable", "stop", "no", "normal"}

# Phase → the one short line the chat gets; planning/done stay quiet to avoid noise.
_PHASE_LINES = {
    "worker": lambda info: f"🧑‍🔧 Worker running ({info['lane']}) · attempt {info['attempt']}/{info['max_attempts']}",
    "review": lambda info: "🔎 Reviewing the worker's changes" + (
        f" ({info['changed_count']} file(s) changed)" if info.get("changed_count") is not None else ""),
    "interrupted": lambda info: "⚠️ Delegate interrupted while a worker was running — its edits may be partial.",
}


def load_delegate_options(config: Optional[dict]) -> tuple[Optional[dict], Optional[str]]:
    """Validated Delegate options from config (``delegate_mode:``) or a human-readable reason."""
    from tui_gateway.prompt_execution_modes import build_execution_spec

    raw = (config or {}).get("delegate_mode")
    if not isinstance(raw, dict) or not raw.get("workers"):
        return None, "Delegate isn't configured: add `delegate_mode.workers` (lane, provider, model, effort) to config.yaml."
    try:
        return build_execution_spec("delegate", raw).options, None
    except ValueError as exc:
        return None, f"Delegate settings in config.yaml are invalid: {exc}"


def phase_line(info: dict) -> Optional[str]:
    make = _PHASE_LINES.get(str(info.get("phase")))
    return make(info) if make else None


def describe(options: dict, enabled: bool) -> str:
    lanes = "\n".join(
        f"• `{w['lane']}` — {w['provider']}/{w['model']}, effort {w.get('effort') or 'default'}"
        for w in options["workers"])
    orch = (options.get("orchestrator") or {}).get("effort") or "default"
    state = "ON for this chat" if enabled else "OFF (Normal)"
    return (f"**Delegate: {state}**\nYou lead with the session model (effort {orch}); workers implement:\n"
            f"{lanes}\n\n`/delegate on` · `/delegate off`")


class GatewayDelegateCommandMixin:
    """Bound onto GatewayRunner: the /delegate slash handler and the per-turn wrapper."""

    def _delegate_enabled(self, session_key: str) -> bool:
        return bool(session_key) and bool(self._session_state(session_key).conversation.delegate_mode)

    async def _handle_delegate_command(self, event: Any) -> Optional[str]:
        import asyncio

        arg = event.get_command_args().strip().lower()
        source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(source)
        options, problem = load_delegate_options(self._load_delegate_config())
        conversation = self._session_state(session_key).conversation
        if arg in _ON:
            if options is None:
                return problem
            conversation.delegate_mode = True
            return describe(options, True)
        if arg in _OFF:
            conversation.delegate_mode = False
            return "Delegate is OFF — back to Normal for this chat."
        if arg:
            return "Usage: `/delegate on`, `/delegate off`, or `/delegate` for status."
        return describe(options, bool(conversation.delegate_mode)) if options else problem

    def _load_delegate_config(self) -> dict:
        from gateway.run import _load_gateway_config
        return _load_gateway_config() or {}
