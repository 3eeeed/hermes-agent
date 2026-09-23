"""Mode intent is immutable and cannot be silently rewritten as Normal in transport."""
import threading
import copy

import pytest

from tui_gateway.prompt_execution_modes import build_execution_spec, require_execution_spec
from tui_gateway.session_auto_continue import _enqueue_prompt


OPTIONS = {
    "side_a": {"provider": "openai", "model": "a"},
    "side_b": {"provider": "openai", "model": "b"},
    "judge": {"provider": "openai", "model": "judge"}, "rounds": 2,
}


def test_server_built_spec_is_immutable_copy_and_refuses_lost_intent():
    incoming = {"execution_mode": "debate", "mode_options": copy.deepcopy(OPTIONS)}
    spec = build_execution_spec(incoming["execution_mode"], incoming["mode_options"])
    incoming["mode_options"]["side_a"]["model"] = "tampered"
    assert spec.mode == "debate"
    assert spec.options["side_a"]["model"] == "a"
    spec.options["side_a"]["model"] = "another"
    assert spec.options["side_a"]["model"] == "a"
    with pytest.raises((AttributeError, TypeError)):
        spec.mode = "normal"
    with pytest.raises(ValueError, match="lost"):
        require_execution_spec(None, expected_mode="debate")


def test_busy_queue_keeps_non_normal_spec_as_separate_envelope():
    session = {"history_lock": threading.RLock(), "inflight_turn": {"user": "live"}}
    spec = build_execution_spec("debate", OPTIONS)
    _enqueue_prompt(session, "first", None, execution_spec=spec)
    _enqueue_prompt(session, "second", None)
    assert session["queued_prompt"]["execution_spec"] is spec
    assert session["queued_prompt"]["text"] == "first"
    assert session["queued_prompts"][0]["text"] == "second"
