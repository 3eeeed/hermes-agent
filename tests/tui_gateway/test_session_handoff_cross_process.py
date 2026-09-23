"""Live two-process proof of the handoff protocol — the bug's actual topology.

A MacBook and an iMac both open Hermes Desktop against ONE remote profile over
SSH. Each Desktop ownershipId starts its own ``hermes serve --isolated``, so the
two writers are separate PROCESSES that share exactly one thing: the profile's
``runtime/active_sessions.json``. Everything in-process (``_sessions``, the
detached-runtime takeover, ``_is_same_writer``) is blind to that split, which is
why the second device could read the chat and never write to it.

The single-process tests pin the algebra; this one pins that the protocol works
when the owner is a real, separate OS process:

* the second process is refused, with the first process's live lease id and
  fencing generation,
* it cannot move the chat while the first process has a turn open — quiescence
  is read from the registry the owner itself wrote, across the process boundary,
* once the owner is idle the compare-and-swap moves the lease, and
* the FIRST process then fails its own validation. That is the fence: nothing
  reached into it, its lease object is intact, and it still must not write.

Spawns real interpreters and synchronises through files, like its sibling
``test_cross_process_orphan_ownership``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli.active_sessions import (
    SESSION_HANDOFF_BUSY,
    SESSION_NOT_OWNED,
    active_session_registry_snapshot,
    handoff_active_session,
    observe_active_session_owner,
    try_acquire_active_session,
)


# The other device's backend: claims the chat, then answers file-based commands.
# It never cooperates with the handoff — the whole point is that the claimant
# needs no cooperation from it beyond what it already writes to the registry.
_OWNER_BACKEND = """
import json
import os
import time
from pathlib import Path

from hermes_cli.active_sessions import (
    begin_active_session_turn,
    end_active_session_turn,
    try_acquire_active_session,
    validate_active_session_lease,
)

session_id = os.environ["SESSION_ID"]
inbox = Path(os.environ["INBOX"])
outbox = Path(os.environ["OUTBOX"])

lease, refusal = try_acquire_active_session(
    session_id=session_id, surface="desktop", config={},
    metadata={"live_session_id": "macbook-runtime"}, track_liveness=True)
assert lease is not None and refusal is None, refusal
Path(os.environ["READY_FILE"]).write_text(
    json.dumps({"lease_id": lease.lease_id, "generation": lease.generation, "pid": os.getpid()}),
    encoding="utf-8")

deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    if not inbox.exists():
        time.sleep(0.02)
        continue
    command = inbox.read_text(encoding="utf-8").strip()
    inbox.unlink()
    if command == "exit":
        break
    if command == "begin_turn":
        refusal = begin_active_session_turn(lease, turn_token="macbook-turn")
        answer = {"ok": refusal is None, "reason": getattr(refusal, "reason", None)}
    elif command == "end_turn":
        end_active_session_turn(lease, turn_token="macbook-turn")
        answer = {"ok": True}
    elif command == "validate":
        refusal = validate_active_session_lease(lease)
        answer = {"ok": refusal is None, "reason": getattr(refusal, "reason", None)}
    else:
        answer = {"ok": False, "reason": "unknown command " + command}
    outbox.write_text(json.dumps(answer), encoding="utf-8")
"""


class _OwnerProcess:
    """File-synchronised handle on the other device's backend."""

    def __init__(self, home: Path, session_id: str, tmp_path: Path):
        self.inbox = tmp_path / "inbox"
        self.outbox = tmp_path / "outbox"
        ready = tmp_path / "owner-ready"
        repo_root = Path(__file__).resolve().parents[2]
        env = {k: v for k, v in os.environ.items()
               if not (k.endswith("_API_KEY") or k.endswith("_TOKEN"))}
        env.update({
            "HERMES_HOME": str(home),
            "PYTHONPATH": os.pathsep.join(
                part for part in (str(repo_root), env.get("PYTHONPATH", "")) if part),
            "SESSION_ID": session_id,
            "READY_FILE": str(ready),
            "INBOX": str(self.inbox),
            "OUTBOX": str(self.outbox),
        })
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _OWNER_BACKEND], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.identity = json.loads(self._await(ready).read_text(encoding="utf-8"))

    def _await(self, path: Path, *, timeout: float = 60.0) -> Path:
        deadline = time.monotonic() + timeout
        while not path.exists():
            if self.proc.poll() is not None:
                stdout, stderr = self.proc.communicate()
                pytest.fail(f"owner backend exited early\nstdout: {stdout}\nstderr: {stderr}")
            if time.monotonic() >= deadline:
                pytest.fail(f"timed out waiting for {path.name}")
            time.sleep(0.02)
        return path

    def ask(self, command: str) -> dict:
        if self.outbox.exists():
            self.outbox.unlink()
        self.inbox.write_text(command, encoding="utf-8")
        answer = json.loads(self._await(self.outbox).read_text(encoding="utf-8"))
        self.outbox.unlink()
        return answer

    def close(self) -> None:
        if self.proc.poll() is None:
            self.inbox.write_text("exit", encoding="utf-8")
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc.communicate()


@pytest.fixture
def owner(tmp_path, monkeypatch):
    """The first device's backend, holding the chat in its own OS process."""
    home = tmp_path / "remote-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    process = _OwnerProcess(home, "20260919_162101_617cd0", tmp_path)
    try:
        yield process
    finally:
        process.close()


def test_a_second_backend_can_be_handed_the_chat_only_when_the_owner_is_idle(owner):
    """The whole user-visible flow, across two real processes.

    Each step is the thing the second device does, in order, with no
    cooperation from the first beyond the registry it already maintains.
    """
    key = "20260919_162101_617cd0"
    assert owner.identity["pid"] != os.getpid(), "the owner must be a separate process"

    # 1. This device tries to write and is refused, with the CAS precondition.
    refused, refusal = try_acquire_active_session(
        session_id=key, surface="desktop", config={},
        metadata={"live_session_id": "imac-runtime"}, track_liveness=True)
    assert refused is None
    assert refusal.reason == SESSION_NOT_OWNED
    assert refusal.ownership["lease_id"] == owner.identity["lease_id"]
    assert refusal.ownership["generation"] == owner.identity["generation"]
    assert refusal.ownership["handoff_supported"] is True

    # 2. The owner starts a turn IN ITS OWN PROCESS. This device must see that.
    assert owner.ask("begin_turn")["ok"] is True
    busy_observation = observe_active_session_owner(key)
    assert busy_observation.busy is True, "quiescence must be readable across the process boundary"

    denied, busy_refusal = handoff_active_session(
        session_id=key, surface="desktop", metadata={"live_session_id": "imac-runtime"},
        expected_lease_id=busy_observation.lease_id,
        expected_generation=busy_observation.generation, track_liveness=True)
    assert denied is None and busy_refusal.reason == SESSION_HANDOFF_BUSY
    assert owner.ask("validate")["ok"] is True, "a refused claim must leave the owner writing"

    # 3. The owner's turn finishes; now the move is allowed.
    assert owner.ask("end_turn")["ok"] is True
    idle_observation = observe_active_session_owner(key)
    assert idle_observation.busy is False

    claimed, refusal = handoff_active_session(
        session_id=key, surface="desktop", metadata={"live_session_id": "imac-runtime"},
        expected_lease_id=idle_observation.lease_id,
        expected_generation=idle_observation.generation, track_liveness=True)
    assert claimed is not None, f"an idle owner must be movable: {refusal}"
    assert claimed.generation == idle_observation.generation + 1

    # 4. The fence, observed from inside the process that used to own the chat.
    fenced = owner.ask("validate")
    assert fenced["ok"] is False
    assert fenced["reason"] == SESSION_NOT_OWNED
    assert owner.ask("begin_turn") == {"ok": False, "reason": SESSION_NOT_OWNED}, (
        "the old backend must not be able to open a turn on a chat it no longer owns"
    )
    assert len(active_session_registry_snapshot()) == 1, "one chat, one writer, throughout"
