"""A pinned pool account must be honoured for every pooled provider, not just Codex.

The desktop account menu pins a credential per session. The turn runner applied
that pin only when the agent's pool was ``openai-codex``, so a Claude session
with several Anthropic accounts always ran on whichever entry the pool happened
to return first — the user's choice in the menu was silently ignored.
"""

from __future__ import annotations

import types

from gateway.run_turn_pool_pin import apply_session_pool_pin


class _Pool:
    def __init__(self, provider, entries):
        self.provider = provider
        self._entries = entries

    def entries(self):
        return self._entries


class _SessionDB:
    """Minimal stand-in for the per-session model-config store."""

    def __init__(self, values=None):
        self.values = values or {}
        self.patched = []

    def get_session_model_config_value(self, session_id, key):
        return self.values.get(key)

    def patch_session_model_config(self, session_id, patch):
        self.patched.append(patch)
        self.values.update(patch)


def _wire(provider, pool_entries, stored):
    """Run the credential-pinning part of turn wiring; return (agent, session_db, swapped)."""
    swapped = []
    agent = types.SimpleNamespace(
        credential_pool=_Pool(provider, pool_entries),
        _swap_credential=lambda entry: swapped.append(entry),
    )
    session_db = _SessionDB(dict(stored))
    runner = types.SimpleNamespace(_session_db=session_db)
    apply_session_pool_pin(agent, runner, "sess-1")
    return agent, session_db, swapped


def test_anthropic_session_honours_pinned_account():
    first = types.SimpleNamespace(id="anth-1")
    second = types.SimpleNamespace(id="anth-2")
    _agent, _db, swapped = _wire(
        "anthropic", [first, second], {"anthropic_credential_id": "anth-2"}
    )
    assert swapped == [second], "the pinned Claude account was not activated"


def test_codex_pinning_still_works():
    first = types.SimpleNamespace(id="cdx-1")
    second = types.SimpleNamespace(id="cdx-2")
    _agent, _db, swapped = _wire(
        "openai-codex", [first, second], {"openai_codex_credential_id": "cdx-2"}
    )
    assert swapped == [second]


def test_rotation_persists_under_the_provider_specific_key():
    entry = types.SimpleNamespace(id="anth-9")
    agent, session_db, _swapped = _wire("anthropic", [entry], {})
    agent.credential_rotation_callback(entry)
    assert session_db.values.get("anthropic_credential_id") == "anth-9", (
        f"rotation wrote the wrong key: {session_db.values}"
    )


def test_pin_for_a_different_provider_is_ignored():
    """A Codex pin must not select an Anthropic entry that happens to share an id."""
    entry = types.SimpleNamespace(id="shared-id")
    _agent, _db, swapped = _wire(
        "anthropic", [entry], {"openai_codex_credential_id": "shared-id"}
    )
    assert swapped == [], "a Codex pin leaked into an Anthropic session"
