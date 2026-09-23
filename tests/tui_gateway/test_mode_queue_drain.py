"""A queued agent-mode prompt keeps its own mode; a queued Normal one never inherits a stale mode."""
import threading

from tui_gateway import server
from tui_gateway.prompt_execution_modes import build_execution_spec

_DEBATE = {"side_a": {"provider": "openrouter", "model": "a"},
           "side_b": {"provider": "openrouter", "model": "b"},
           "judge": {"provider": "openrouter", "model": "judge"}, "rounds": 1}


def _session(**extra):
    return {"history_lock": threading.RLock(), "running": False, "history": [], "history_version": 0,
            "agent": None, "attached_images": [], "_queued_prompt_generation": 0, **extra}


def test_debate_queue_drain_passes_original_spec_to_worker(monkeypatch):
    spec = build_execution_spec("debate", _DEBATE)
    session = _session(queued_prompt={"text": "debate this", "transport": None, "execution_spec": spec})
    dispatched = []
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **kw: False)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **kw:
                        dispatched.append((kw.get("execution_spec"), a[2].get("_execution_spec"))) or True)
    assert server._drain_queued_prompt("rid", "sid", session) is True
    assert dispatched == [(spec, spec)]


def test_drain_normal_after_debate_replaces_stale_mode(monkeypatch):
    stale = build_execution_spec("debate", _DEBATE)
    captured = {}
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **kw: False)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *args, **kwargs: captured.update(
        mode=args[2].get("_execution_mode"), spec=kwargs.get("execution_spec")) or True)
    session = _session(queued_prompt={"text": "Normal next", "transport": None},
                       _execution_mode="debate", _execution_spec=stale)
    assert server._drain_queued_prompt("r1", "sid", session) is True
    assert captured == {"mode": None, "spec": None}


def test_moded_prompt_is_never_merged_into_a_normal_envelope():
    from tui_gateway.session_auto_continue import _enqueue_prompt
    spec = build_execution_spec("debate", _DEBATE)
    session = _session()
    _enqueue_prompt(session, "plain", None)
    _enqueue_prompt(session, "debate this", None, execution_spec=spec)
    assert session["queued_prompt"]["text"] == "plain"
    assert [e["text"] for e in session["queued_prompts"]] == ["debate this"]
    assert session["queued_prompts"][0]["execution_spec"] is spec
