"""Per-session pooled-account pin for gateway turns.

The desktop account menu pins one pooled credential per session (stored in the
session's model_config under the provider's ``POOL_PIN_KEYS`` key). The pin is
re-applied at the start of every turn, including when the AIAgent is cached, so
it never changes a request already in flight.
"""

from __future__ import annotations

from typing import Any


def _runner_session_db(runner: Any) -> Any:
    """Resolved lazily: turn wiring runs for unpooled providers too, and those
    runners need not carry a session store at all."""
    store = getattr(runner, "_session_db", None)
    return getattr(store, "_db", store)


def apply_session_pool_pin(agent: Any, runner: Any, session_id: str | None) -> None:
    """Activate the session's pinned account and persist rotations back to the pin.

    Keyed per provider so a Codex pin can never select an Anthropic entry (and
    vice versa) when the opaque ids collide.
    """
    from agent.credential_pool import POOL_PIN_KEYS

    pool = getattr(agent, "credential_pool", None) or getattr(agent, "_credential_pool", None)
    pin_key = POOL_PIN_KEYS.get(getattr(pool, "provider", "") if pool is not None else "")

    if pin_key and session_id:
        store = _runner_session_db(runner)
        selected_credential_id = (
            store.get_session_model_config_value(session_id, pin_key) if store else None
        )
        if selected_credential_id and pool is not None:
            selected_entry = next(
                (entry for entry in pool.entries() if entry.id == selected_credential_id), None
            )
            if selected_entry is not None:
                agent._swap_credential(selected_entry)

    def persist_rotated_credential(entry) -> None:
        if not (session_id and pin_key):
            return
        store = _runner_session_db(runner)
        if store is not None:
            store.patch_session_model_config(session_id, {pin_key: entry.id})

    agent.credential_rotation_callback = persist_rotated_credential
