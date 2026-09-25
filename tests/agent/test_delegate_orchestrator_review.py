"""Delegate review contract: a worker result carries only the files that changed during
that attempt, and the orchestrator cannot answer before checking the latest result."""
import json
import subprocess

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
