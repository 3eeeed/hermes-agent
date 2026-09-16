"""A pinned pool account must be honoured for every pooled provider, not just Codex.

The desktop account menu pins a credential per session. The turn runner applied
that pin only when the agent's pool was ``openai-codex``, so a Claude session
with several Anthropic accounts always ran on whichever entry the pool happened
to return first — the user's choice in the menu was silently ignored.
"""

from __future__ import annotations

import types

from gateway.run_turn_runner import TurnRunner


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
    ctx = types.SimpleNamespace(
        session_id="sess-1",
        progress_callback=None,
        native_tool_start_callback=None,
        voice_ack_callback=None,
        _voice_ack_guild=[None],
        _native_slack_task_cards=False,
        native_tool_complete_callback=None,
        _step_callback_sync=None,
        _hooks_ref=types.SimpleNamespace(loaded_hooks=[]),
        _status_callback_sync=None,
        _event_callback_sync=None,
        _status_adapter=None,
        session_key="",
        user_config={},
        _thinking_enabled=False,
        agent_holder=[None],
        tools_holder=[None],
        process_task_id=None,
        process_baseline=None,
        run_generation=0,
    )
    holder = types.SimpleNamespace(
        _ctx=ctx,
        _runner=types.SimpleNamespace(
            _service_tier=None,
            _session_db=session_db,
            _consume_pending_turn_sidecar_notes=lambda key: [],
        ),
        _make_bg_review_callbacks=lambda: (lambda message: None, lambda: None),
        _merge_turn_request_overrides=TurnRunner._merge_turn_request_overrides,
        _clarify_callback_sync=lambda *a, **k: None,
        _notice_callback_sync=lambda *a, **k: None,
        _attach_session_title_callback=lambda agent, ctx: None,
    )
    TurnRunner._wire_turn_agent_callbacks(holder, agent, {}, None, None, None, False)
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
