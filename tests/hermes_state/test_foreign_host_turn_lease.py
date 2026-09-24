"""Host-scoped PID proof for cross-process transcript turn leases."""
import os
import subprocess
import sys

from hermes_state import SessionDB
from hermes_state_send_ledger import prompt_send_host_identity


def test_foreign_host_dead_local_pid_cannot_reclaim_unexpired_turn_lease(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("shared", source="test")
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        foreign = f"host={'f' * 24}:pid={dead.pid}:turn=foreign"
        local = f"host={prompt_send_host_identity()}:pid={os.getpid()}:turn=local"
        assert db.try_acquire_session_turn_lease("shared", foreign, ttl_seconds=300)
        assert not db.try_acquire_session_turn_lease("shared", local, ttl_seconds=300)
        assert db.refresh_session_turn_lease("shared", foreign)
    finally:
        db.close()


def test_local_host_dead_pid_can_reclaim_unexpired_turn_lease(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("shared", source="test")
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        local = f"host={prompt_send_host_identity()}:pid={dead.pid}:turn=old"
        successor = f"host={prompt_send_host_identity()}:pid={os.getpid()}:turn=new"
        assert db.try_acquire_session_turn_lease("shared", local, ttl_seconds=300)
        assert db.try_acquire_session_turn_lease("shared", successor, ttl_seconds=300)
    finally:
        db.close()
