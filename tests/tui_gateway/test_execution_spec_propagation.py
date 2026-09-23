"""Mode intent is immutable and cannot be silently rewritten as Normal in transport."""
import threading
import copy
import io
import json

import pytest

from tui_gateway.prompt_execution_modes import build_execution_spec, require_execution_spec
from tui_gateway.session_auto_continue import _enqueue_prompt
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost


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


def test_isolated_frame_serializes_exact_non_normal_spec():
    spec = build_execution_spec("debate", OPTIONS)
    session = {"history_lock": threading.RLock(), "history": [], "_execution_spec": spec,
               "_execution_mode": "debate", "session_key": "stored", "cols": 80}
    frame = server._compute_host_turn_frame("rid", "sid", session, "question", execution_spec=spec)
    assert frame["execution_mode"] == "debate"
    assert frame["execution_spec"] == spec.as_wire()
    assert require_execution_spec(frame.get("execution_spec"), expected_mode=frame["execution_mode"]) == spec


def test_child_refuses_missing_or_unavailable_spec_before_creating_session():
    spec = build_execution_spec("debate", OPTIONS)
    for payload, word in ((None, "lost"), (spec.as_wire(), "unavailable")):
        out = io.StringIO()
        host = ComputeHost(stdout=out, heartbeat_secs=0)
        frame = {"type": "turn.start", "sid": "missing-mode-session", "request_id": "test",
                 "execution_mode": "debate", "text": "hello"}
        if payload is not None:
            frame["execution_spec"] = payload
        host._run_real_turn(frame)
        emitted = [json.loads(line) for line in out.getvalue().splitlines()]
        assert any(f["type"] == "turn.error" and word in f["message"] for f in emitted), emitted
        assert "missing-mode-session" not in server._sessions
        host.close()
