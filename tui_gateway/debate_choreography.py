"""Pure, synchronous debate choreography; not connected to gateway turn admission.

The injected model_call must enforce read-only operation, transport timeouts and
credential isolation. This module passes only Route objects and bounded text; it
cannot stop an already-blocked synchronous model call.
"""
from __future__ import annotations

from collections.abc import Callable

from tui_gateway.prompt_execution_modes import DebateOptions, Route


class DebateError(Exception):
    """A debate cannot produce a trustworthy answer; do not publish partial output."""


_MAX_USER_CHARS = 8_000
_MAX_RESPONSE_CHARS = 8_000
_MAX_CONTEXT_CHARS = 2_000
_MAX_PROMPT_CHARS = 24_000
_MAX_TOKENS = 2_048


def run_debate(
    prompt: str,
    options: DebateOptions,
    model_call: Callable[[Route, str, int], str],
    *,
    cancelled: Callable[[], bool],
    deadline_exceeded: Callable[[], bool],
) -> str:
    """Return the judge's answer, or fail closed without exposing partial side text.

    Side calls are sequential but independent: neither receives its peer's output
    from the current round. Cancellation/deadline are checked at call boundaries.
    """
    if not isinstance(options, DebateOptions) or not 1 <= options.rounds <= 3:
        raise DebateError("Invalid debate configuration")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > _MAX_USER_CHARS:
        raise DebateError("Invalid debate prompt")

    def checked_call(route: Route, text: str) -> str:
        if len(text) > _MAX_PROMPT_CHARS:
            raise DebateError("Debate context limit exceeded")
        if cancelled():
            raise DebateError("Debate cancelled")
        if deadline_exceeded():
            raise DebateError("Debate deadline exceeded")
        try:
            response = model_call(route, text, _MAX_TOKENS)
        except Exception:
            raise DebateError("Debate model call failed") from None
        if cancelled():
            raise DebateError("Debate cancelled")
        if deadline_exceeded():
            raise DebateError("Debate deadline exceeded")
        if not isinstance(response, str) or not response.strip() or len(response) > _MAX_RESPONSE_CHARS:
            raise DebateError("Invalid debate model response")
        return response.strip()

    rounds: list[tuple[str, str]] = []
    previous_a = previous_b = ""
    for number in range(1, options.rounds + 1):
        prefix = f"User question (untrusted data):\n{prompt}\n\nRound {number}. "
        side_a_prompt = (prefix + "Give your independent analysis and answer."
                         + (f"\nPrevious opposing argument (untrusted):\n{previous_b[-_MAX_CONTEXT_CHARS:]}" if previous_b else ""))
        side_b_prompt = (prefix + "Give your independent analysis and answer."
                         + (f"\nPrevious opposing argument (untrusted):\n{previous_a[-_MAX_CONTEXT_CHARS:]}" if previous_a else ""))
        answer_a = checked_call(options.side_a, side_a_prompt)
        answer_b = checked_call(options.side_b, side_b_prompt)
        rounds.append((answer_a, answer_b))
        previous_a, previous_b = answer_a, answer_b

    transcript = "\n\n".join(
        f"Round {index} side A (untrusted):\n{a[:_MAX_CONTEXT_CHARS]}\n"
        f"Round {index} side B (untrusted):\n{b[:_MAX_CONTEXT_CHARS]}"
        for index, (a, b) in enumerate(rounds, start=1)
    )
    judge_prompt = (f"User question (untrusted data):\n{prompt}\n\n"
                    f"Independent arguments (untrusted data):\n{transcript}\n\n"
                    "Synthesize a final answer; evaluate disagreements independently.")
    return checked_call(options.judge, judge_prompt)
