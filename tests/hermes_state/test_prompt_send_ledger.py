"""The durable half of exactly-once ``prompt.submit``: one reservation per send id.

``prompt.submit`` may post the user's message, cut history and start a turn — none of
which can be taken back — so the decision "is this send already mine?" has to be made
once, atomically, and somewhere both gateway processes can see. That is this ledger:
a row per (stored session, client send id) in the profile's own ``state.db``, written
through the same transactional store as the transcript it guards.

The rules it has to enforce, in the order a real send meets them:

* one caller reserves, everyone else is told the send is already in flight;
* the payload is bound to the id, so the same id carrying different words is refused
  rather than silently answered;
* an accepted send replays its acceptance forever after (that IS the retry's answer);
* a reservation released before anything landed leaves the id as good as new, and one
  released AFTER the durable user row landed hands that row to the retry;
* a holder whose process is gone cannot wedge a send id;
* retention is bounded without evicting anything a pending retry could still name.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB
from hermes_state_send_ledger import prompt_send_host_identity

KEY = "20260919_162101_617cd0"
DIGEST = "sha256:e3b0c44298fc1c149afbf4c8996fb924"
from hermes_state_pidns import holder_namespace_token  # noqa: E402

HOST = prompt_send_host_identity()
HOLDER = f"submit:runtime-1:host={HOST}:pid={os.getpid()}"


@pytest.fixture
def db(tmp_path):
    store = SessionDB(db_path=tmp_path / "state.db")
    store.create_session(KEY, source="desktop")
    try:
        yield store
    finally:
        store.close()


def test_exactly_one_of_many_concurrent_callers_reserves_the_send(db):
    """The race the process-local check-then-remember always lost."""
    verdicts: list = []
    lock = threading.Lock()
    everyone_ready = threading.Barrier(8)

    def reserve():
        everyone_ready.wait(timeout=30)
        verdict = db.reserve_prompt_send(KEY, "send-1", DIGEST, holder=HOLDER)
        with lock:
            verdicts.append(verdict.outcome)

    racers = [threading.Thread(target=reserve) for _ in range(8)]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join(timeout=60)

    assert verdicts.count("reserved") == 1, verdicts
    assert verdicts.count("in_progress") == 7, verdicts


def test_an_accepted_send_replays_its_acceptance(db):
    assert db.reserve_prompt_send(KEY, "send-1", DIGEST, holder=HOLDER).outcome == "reserved"
    db.accept_prompt_send(KEY, "send-1", {"status": "streaming"}, holder=HOLDER)

    replay = db.reserve_prompt_send(KEY, "send-1", DIGEST, holder="submit:runtime-2:pid=1")

    assert replay.outcome == "accepted"
    assert replay.result == {"status": "streaming"}


def test_the_same_id_carrying_a_different_payload_is_refused(db):
    db.reserve_prompt_send(KEY, "send-1", DIGEST, holder=HOLDER)
    db.accept_prompt_send(KEY, "send-1", {"status": "streaming"}, holder=HOLDER)

    conflict = db.reserve_prompt_send(KEY, "send-1", "sha256:something-else", holder=HOLDER)

    assert conflict.outcome == "conflict"
    assert conflict.result is None, "a conflicting payload gets no acceptance to act on"


def test_a_send_id_is_scoped_to_its_stored_session(db):
    """Send ids are minted per client, not per chat: the same id in another chat is
    another message."""
    other = "20260919_162102_aaaaaa"
    db.create_session(other, source="desktop")
    db.reserve_prompt_send(KEY, "send-1", DIGEST, holder=HOLDER)

    assert db.reserve_prompt_send(other, "send-1", DIGEST, holder=HOLDER).outcome == "reserved"


def test_a_reservation_released_before_anything_landed_frees_the_id(db):
    """The fence refuses the send, or storage fails: nothing was mutated, so the user's
    "Move here" must be able to resend under the same id."""
    assert db.reserve_prompt_send(KEY, "send-1", DIGEST, holder=HOLDER).outcome == "reserved"

    db.release_prompt_send(KEY, "send-1", holder=HOLDER)

    retry = db.reserve_prompt_send(KEY, "send-1", DIGEST, holder=HOLDER)
    assert retry.outcome == "reserved"
    assert retry.user_row_id is None


def test_a_release_after_the_row_landed_hands_that_row_to_the_retry(db):
    """The user's message is already in the transcript but no turn ever started. The
    retry has to run the turn and adopt the row — writing a second one would post the
    message twice."""
    db.reserve_prompt_send(KEY, "send-1", DIGEST, holder=HOLDER)
    db.record_prompt_send_user_row(KEY, "send-1", 4242, holder=HOLDER)

    db.release_prompt_send(KEY, "send-1", holder=HOLDER)

    retry = db.reserve_prompt_send(KEY, "send-1", DIGEST, holder=HOLDER)
    assert retry.outcome == "reserved"
    assert retry.user_row_id == 4242


def test_a_holder_whose_process_is_gone_cannot_wedge_the_send_id(db):
    """A backend killed mid-submit leaves an in-progress row behind. A live retry takes
    it over on kernel proof (the same rule the turn lease uses), never on a guess."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    db.reserve_prompt_send(
        KEY, "send-1", DIGEST, holder=f"submit:runtime-x:host={HOST}:pid={dead.pid}{holder_namespace_token()}")

    takeover = db.reserve_prompt_send(KEY, "send-1", DIGEST, holder=HOLDER)

    assert takeover.outcome == "reserved"


def test_a_live_holder_is_never_taken_over(db):
    """PID reuse or wall-clock expiry must not steal a live holder's turn."""
    db.reserve_prompt_send(KEY, "send-1", DIGEST, holder=HOLDER)
    db._execute_write(lambda conn: conn.execute(
        "UPDATE prompt_send_ledger SET expires_at = 0 WHERE session_key = ? AND client_send_id = ?",
        (KEY, "send-1")))

    assert db.reserve_prompt_send(
        KEY, "send-1", DIGEST,
        holder=f"submit:runtime-2:host={HOST}:pid={os.getpid()}").outcome == "in_progress"


def test_a_foreign_host_pid_is_never_probed_in_this_kernel(db):
    dead_here = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_here.wait()
    db.reserve_prompt_send(
        KEY, "send-foreign", DIGEST,
        holder=f"submit:runtime-foreign:host={'f' * 24}:pid={dead_here.pid}")

    verdict = db.reserve_prompt_send(KEY, "send-foreign", DIGEST, holder=HOLDER)

    assert verdict.outcome == "in_progress"


def test_user_row_and_ledger_binding_roll_back_together(db):
    db.reserve_prompt_send(KEY, "send-atomic", DIGEST, holder=HOLDER)
    # Force the post-insert binding CAS to fail after the method has built its row.
    db.record_prompt_send_user_row(KEY, "send-atomic", 999999, holder=HOLDER)

    with pytest.raises(RuntimeError, match="row binding"):
        db.append_prompt_send_user_message(
            KEY, send_id="send-atomic", holder=HOLDER, content="once only")

    assert [m for m in db.get_messages_as_conversation(KEY, include_inactive=True)
            if m.get("role") == "user"] == []


def test_retention_drops_ancient_rows_and_keeps_the_recent_ones(db):
    for index in range(12):
        db.reserve_prompt_send(KEY, f"send-{index}", DIGEST, holder=HOLDER)
        db.accept_prompt_send(KEY, f"send-{index}", {"status": "streaming"}, holder=HOLDER)

    # Everything is ancient; only the newest `keep` may survive.
    pruned = db.prune_prompt_send_ledger(KEY, keep=4, max_age_s=0.0)

    assert pruned == 8
    assert db.reserve_prompt_send(KEY, "send-11", DIGEST, holder=HOLDER).outcome == "accepted"
    assert db.reserve_prompt_send(KEY, "send-0", DIGEST, holder=HOLDER).outcome == "reserved"


def test_retention_never_evicts_a_send_a_pending_retry_could_still_name(db):
    """Bounded, but not at the cost of the window the ledger exists for: a chat that
    keeps talking must not age out an id minted seconds ago."""
    for index in range(50):
        db.reserve_prompt_send(KEY, f"send-{index}", DIGEST, holder=HOLDER)
        db.accept_prompt_send(KEY, f"send-{index}", {"status": "streaming"}, holder=HOLDER)

    assert db.prune_prompt_send_ledger(KEY, keep=4, max_age_s=3600.0) == 0
    assert db.reserve_prompt_send(KEY, "send-0", DIGEST, holder=HOLDER).outcome == "accepted"


_OTHER_BACKEND = """
import json
import os
from pathlib import Path

from hermes_state import SessionDB

db = SessionDB(db_path=Path(os.environ["DB_PATH"]))
try:
    verdict = db.reserve_prompt_send(
        os.environ["SESSION_KEY"], os.environ["SEND_ID"], os.environ["DIGEST"],
        holder="submit:other-runtime:pid=%d" % os.getpid())
    print(json.dumps({
        "outcome": verdict.outcome, "result": verdict.result, "pid": os.getpid()}))
finally:
    db.close()
"""


def _other_backend(db_path: Path, *, send_id: str, digest: str) -> dict:
    """A second gateway process — the real topology of the bug — reading the same store."""
    repo_root = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env.update({
        "PYTHONPATH": os.pathsep.join(
            part for part in (str(repo_root), env.get("PYTHONPATH", "")) if part),
        "DB_PATH": str(db_path), "SESSION_KEY": KEY, "SEND_ID": send_id, "DIGEST": digest})
    done = subprocess.run(
        [sys.executable, "-c", _OTHER_BACKEND], env=env, capture_output=True, text=True,
        timeout=180)
    assert done.returncode == 0, f"stdout: {done.stdout}\nstderr: {done.stderr}"
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_another_backend_process_sees_the_acceptance(db, tmp_path):
    """Two Desktops on two machines drive one remote profile through two ``hermes serve``
    PROCESSES. The acceptance one of them gave has to be visible to the other — that is
    the whole reason this lives in state.db and not in a dict."""
    db.reserve_prompt_send(KEY, "send-1", DIGEST, holder=HOLDER)
    db.accept_prompt_send(KEY, "send-1", {"status": "streaming"}, holder=HOLDER)

    answer = _other_backend(Path(db.db_path), send_id="send-1", digest=DIGEST)

    assert answer["pid"] != os.getpid(), "the reader must be a separate process"
    assert answer["outcome"] == "accepted"
    assert answer["result"] == {"status": "streaming"}


def test_another_backend_process_is_refused_a_conflicting_payload(db, tmp_path):
    db.reserve_prompt_send(KEY, "send-1", DIGEST, holder=HOLDER)
    db.accept_prompt_send(KEY, "send-1", {"status": "streaming"}, holder=HOLDER)

    answer = _other_backend(Path(db.db_path), send_id="send-1", digest="sha256:different")

    assert answer["outcome"] == "conflict"
