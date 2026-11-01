"""Delegate review contract: a worker result carries only the files that changed during
that attempt, and the orchestrator cannot answer before checking the latest result."""
import json
import subprocess
from types import SimpleNamespace

from agent import delegate_orchestrator as orch

OPTIONS = {"workers": [{"lane": "impl", "provider": "p", "model": "m"}]}


class _Agent:
    reasoning_config = None


def _repo(tmp_path):
    subprocess.run("git init -q && echo a > a.txt && echo keep > pre.txt && git add a.txt && "
                   "git -c user.email=t@t -c user.name=t commit -qm init && echo dirty >> pre.txt",
                   shell=True, check=True, cwd=tmp_path)
    return tmp_path


def test_worker_evidence_lists_only_its_own_changes_and_gates_the_answer(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("TERMINAL_CWD", str(repo))
    agent = _Agent()
    with orch.orchestrator_turn(agent, "turn-1", OPTIONS):
        assert orch.verify_nudge(agent) is None  # nothing delegated yet: answering is allowed
        assert orch.before_dispatch(agent) is None
        (repo / "a.txt").write_text("changed\n")
        (repo / "new.txt").write_text("new\n")
        (repo / "__pycache__").mkdir()
        (repo / "__pycache__" / "x.pyc").write_bytes(b"0")
        result = json.loads(orch.after_dispatch(agent, json.dumps({"results": [{"summary": "done"}]})))
        observed = result["delegate_review"]["observed_changes"]
        # pre.txt was already dirty before the worker started; cache output is not a change.
        assert observed["changed_paths"] == ["a.txt", "new.txt"]

        assert orch.verify_nudge(agent)  # must check before answering
        assert orch.verify_nudge(agent) is None  # nudged once, never loops
        assert orch.before_dispatch(agent) is None
        orch.after_dispatch(agent, "{}")
        orch.blocked_message("terminal", "turn-1")  # the orchestrator ran a check
        assert orch.verify_nudge(agent) is None

        orch.before_dispatch(agent)
        refusal = orch.before_dispatch(agent)
        assert refusal and "budget" in refusal  # a first attempt plus two corrections

    assert orch.blocked_message("patch", "turn-1") is None  # nothing leaks past the turn
    assert orch.verify_nudge(agent) is None


def test_phases_report_progress_and_a_stopped_worker_is_interrupted(tmp_path, monkeypatch):
    _repo(tmp_path)
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    options = {"workers": [{"lane": "code", "provider": "anthropic", "model": "m"}]}

    def run(stop_midway):
        phases = []
        agent = SimpleNamespace(reasoning_config=None, _interrupt_requested=False)
        with orch.orchestrator_turn(agent, "t", options, on_phase=lambda info: phases.append(info["phase"])):
            orch.routing_for_tasks(agent, [{"goal": "x"}])
            orch.before_dispatch(agent)
            if stop_midway:
                pass  # the worker never returned: the turn ended under it
            else:
                orch.after_dispatch(agent, "{}")
        return phases

    assert run(False) == ["planning", "worker", "review", "done"]
    assert run(True) == ["planning", "worker", "interrupted"]

    # Stopped after the worker returned: the finalizer already cleared the agent's own flag,
    # so the gateway marks the turn from its result.
    phases = []
    agent = SimpleNamespace(reasoning_config=None, _interrupt_requested=False)
    with orch.orchestrator_turn(agent, "t", options, on_phase=lambda info: phases.append(info["phase"])) as turn:
        orch.routing_for_tasks(agent, [{"goal": "x"}])
        orch.before_dispatch(agent)
        orch.after_dispatch(agent, "{}")
        turn.interrupted = True
    assert phases[-1] == "interrupted"


def test_review_outside_git_reports_unknown_count_not_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))  # plain folder, no git
    options = {"workers": [{"lane": "code", "provider": "anthropic", "model": "m"}]}
    seen = []
    agent = SimpleNamespace(reasoning_config=None, _interrupt_requested=False)
    with orch.orchestrator_turn(agent, "t", options, on_phase=seen.append):
        orch.routing_for_tasks(agent, [{"goal": "x"}])
        orch.before_dispatch(agent)
        orch.after_dispatch(agent, "{}")
    review = next(info for info in seen if info["phase"] == "review")
    assert review["changed_count"] is None

    _repo(tmp_path)  # inside git the count is real
    seen.clear()
    with orch.orchestrator_turn(agent, "t", options, on_phase=seen.append):
        orch.routing_for_tasks(agent, [{"goal": "x"}])
        orch.before_dispatch(agent)
        (tmp_path / "new.txt").write_text("x")
        orch.after_dispatch(agent, "{}")
    review = next(info for info in seen if info["phase"] == "review")
    assert review["changed_count"] == 1
