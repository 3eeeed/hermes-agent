"""Bounded conversation excerpt for Debate/Delegate advisory calls.

Advisory models do not receive the main agent's history. Without an excerpt a
follow-up such as "I sent it from the MacBook" is answered as a context-free
question. Only visible user/assistant text is carried; tool traffic, system
notices and the current prompt are excluded, and the newest turns win.
"""
from __future__ import annotations

from typing import Any

MAX_CONTEXT_CHARS = 3_000
_MAX_MESSAGE_CHARS = 1_200
_HIDDEN_KINDS = frozenset({"auto_continue", "model_switch", "async_delegation_complete"})


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [p.get("text") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
    return ""


def conversation_excerpt(history: Any, current_prompt: str, *, limit: int = MAX_CONTEXT_CHARS) -> str:
    """Return the newest visible turns before ``current_prompt`` within ``limit`` chars."""
    if not isinstance(history, list):
        return ""
    turns: list[tuple[str, str]] = []
    for message in history:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            continue
        if message.get("display_kind") in _HIDDEN_KINDS or message.get("tool_calls"):
            continue
        text = _text(message.get("content"))
        if text:
            turns.append((message["role"], text))
    if turns and turns[-1] == ("user", current_prompt.strip()):
        turns.pop()  # The submit row may already be in history; it is sent separately.
    lines: list[str] = []
    used = 0
    for role, text in reversed(turns):
        if len(text) > _MAX_MESSAGE_CHARS:
            text = text[:_MAX_MESSAGE_CHARS] + " …"
        line = f"{'User' if role == 'user' else 'Assistant'}: {text}"
        if used + len(line) + 1 > limit:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(reversed(lines))


def with_context(prompt: str, context: str) -> str:
    """Prefix the untrusted-data question block with the recent conversation."""
    if not context:
        return f"User question (untrusted data):\n{prompt}"
    return (f"Recent conversation (untrusted data, oldest first):\n{context}\n\n"
            f"Latest user message (untrusted data):\n{prompt}")
