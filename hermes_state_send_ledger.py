"""Durable exactly-once ledger for ``prompt.submit`` (SessionDB mixin).

A submit is not a read: it posts the user's message into the transcript, can cut
history, and starts a turn. None of that can be taken back, so a client that retries a
send whose RESPONSE was lost — the ordinary shape of a dropped WebSocket, and exactly
the socket a "Move here" resend rides — must be recognised rather than obeyed twice.

The recognition has to survive everything the retry does not:

* **Two requests at once.** Reserve is one ``BEGIN IMMEDIATE`` transaction: read the row,
  decide, write the decision. There is no window between "have I seen this?" and
  "remember it" for a second caller to slip through.
* **Two processes.** Two Desktops on two machines reach one remote profile through two
  ``hermes serve --isolated`` processes; the only thing they share is the profile's
  ``state.db``. That is why the ledger is a table and not a dict.
* **A restart.** The row outlives the runtime session id, the live session record and the
  backend itself, so the key is the STORED session id — never a transient runtime id.
* **A wrong retry.** ``payload_digest`` binds the id to the message it was minted for: the
  same id carrying different words is refused (``conflict``), never answered with an
  acceptance that belongs to another message.

States: ``in_progress`` (reserved by a live holder) → ``accepted`` (terminal; its stored
result IS the answer every later retry gets) or released — deleted when nothing durable
landed, else kept as ``failed`` carrying the ``user_row_id`` the attempt already wrote so
the retry adopts that row instead of posting the message a second time.
"""

from __future__ import annotations

import json
import functools
import hashlib
import logging
import re
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def prompt_send_host_identity() -> str:
    """Stable local-host fingerprint used only to scope kernel PID proof.

    A state DB may be shared by gateways on different machines; a PID absent in
    this kernel says nothing about a live process on another host. Combine the
    OS machine id (when present), hostname and hardware node, then expose only a
    hash in the ledger holder string.
    """
    machine_id = ""
    for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            machine_id = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if machine_id:
            break
    raw = f"{machine_id}|{socket.gethostname()}|{uuid.getnode():012x}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

#: How long an unfinished reservation stays authoritative for a holder this process cannot
#: prove is gone (a foreign host, a recycled pid). Generous next to a submit's pre-accept
#: work (persist + thread start, each with its own write patience) and short enough that a
#: wedged id heals on its own. A crashed LOCAL holder is reclaimed at once by pid proof.
PROMPT_SEND_RESERVATION_TTL_S = 300.0

#: Retention: a send id stays recognisable for a day, and the newest ``KEEP`` per chat
#: always survive. Both bounds together — a count-only bound lets a chatty session evict
#: the very id a pending "Move here" is about to retry, which is replay resurrection.
PROMPT_SEND_LEDGER_KEEP = 64
PROMPT_SEND_LEDGER_MAX_AGE_S = 86400.0

_IN_PROGRESS, _ACCEPTED, _FAILED = "in_progress", "accepted", "failed"

_LEDGER_ROW_SQL = (
    "SELECT payload_digest, state, holder, user_row_id, result_json, expires_at "
    "FROM prompt_send_ledger WHERE session_key = ? AND client_send_id = ?")


@dataclass(frozen=True)
class PromptSendVerdict:
    """What a caller may do with this send id.

    ``reserved`` — it is yours; nothing else has run this message.
    ``accepted`` — already done: answer the client with ``result``.
    ``in_progress`` — another live holder is running it; do not start a second turn.
    ``conflict`` — this id belongs to a different message; refuse, mutate nothing.
    """

    outcome: str
    result: Optional[Dict[str, Any]] = None
    #: Durable user row a previous attempt at THIS send already wrote, when there is one.
    user_row_id: Optional[int] = None
    #: The ledger state the verdict was read from ("" for a send never seen before).
    state: str = ""


def _decoded_result(raw) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("prompt send ledger: unreadable stored result; replaying as a bare acceptance")
        return None
    return decoded if isinstance(decoded, dict) else None


class SessionSendLedgerMixin:
    """See module docstring — mixin for SessionDB (prompt send ledger cluster)."""

    def _prompt_send_holder_is_gone(self, holder: str, expires_at, now: float) -> bool:
        """True only when the kernel proves the reservation process is gone.

        Wall-clock expiry is not proof that an accepted-but-unsettled worker stopped;
        taking over a live holder after a fixed TTL can execute the same turn twice.
        All ordinary pre-dispatch failures explicitly release to ``failed``/delete,
        while a crashed local gateway is recoverable immediately by PID proof.
        """
        from hermes_state import _compression_lock_holder_process_is_dead
        host = re.search(r"(?:^|:)host=([0-9a-f]+)(?::|$)", holder or "")
        if host is None or host.group(1) != prompt_send_host_identity():
            return False
        return _compression_lock_holder_process_is_dead(holder or "")

    def reserve_prompt_send(
        self, session_key: str, send_id: str, payload_digest: str, *, holder: str,
        ttl_seconds: float = PROMPT_SEND_RESERVATION_TTL_S,
        prune_keep: int = PROMPT_SEND_LEDGER_KEEP,
        prune_max_age_s: float = PROMPT_SEND_LEDGER_MAX_AGE_S,
    ) -> PromptSendVerdict:
        """Claim ``send_id`` for ``session_key`` before the caller mutates anything.

        Read, decide and write share ONE write transaction, so concurrent callers — in
        this process or another — cannot both come back ``reserved``. Retention runs in
        that same transaction when a genuinely new send is recorded: no extra lock, and
        the table cannot grow without a new message arriving to bound it.
        """
        if not session_key or not send_id or not payload_digest:
            raise ValueError("reserve_prompt_send needs a stored session id, a send id and a digest")
        now = time.time()
        expires_at = now + max(1.0, float(ttl_seconds))

        def _do(conn):
            row = conn.execute(_LEDGER_ROW_SQL, (session_key, send_id)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO prompt_send_ledger (session_key, client_send_id, payload_digest, "
                    "state, holder, created_at, updated_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (session_key, send_id, payload_digest, _IN_PROGRESS, holder, now, now, expires_at))
                self._prune_prompt_send_ledger_on_conn(
                    conn, session_key, keep=prune_keep, max_age_s=prune_max_age_s, now=now)
                return PromptSendVerdict("reserved")
            stored_digest, state, row_holder, user_row_id, result_json, row_expires = (
                row[0], row[1], row[2], row[3], row[4], row[5])
            if stored_digest != payload_digest:
                # Fail closed: one id, one message. Nothing is written — a conflicting
                # retry must leave the send it collided with exactly as it found it.
                return PromptSendVerdict("conflict", state=state)
            if state == _ACCEPTED:
                return PromptSendVerdict(
                    "accepted", result=_decoded_result(result_json), user_row_id=user_row_id,
                    state=state)
            if state == _IN_PROGRESS and not self._prompt_send_holder_is_gone(
                    row_holder, row_expires, now):
                return PromptSendVerdict("in_progress", user_row_id=user_row_id, state=state)
            # Released after a durable row landed, or abandoned by a holder that is gone:
            # this caller takes it over, inheriting the row so it posts nothing twice.
            conn.execute(
                "UPDATE prompt_send_ledger SET state = ?, holder = ?, updated_at = ?, "
                "expires_at = ? WHERE session_key = ? AND client_send_id = ?",
                (_IN_PROGRESS, holder, now, expires_at, session_key, send_id))
            return PromptSendVerdict("reserved", user_row_id=user_row_id, state=state)

        return self._execute_write(_do)

    def record_prompt_send_user_row(
        self, session_key: str, send_id: str, user_row_id: int, *, holder: str) -> bool:
        """Remember the durable user row this reservation wrote.

        Written as soon as the row lands, not at the end: if the backend dies between the
        two, the retry still finds the row and adopts it instead of posting the user's
        message a second time. Only the holder may stamp its own reservation."""
        if not session_key or not send_id or user_row_id is None:
            return False
        return bool(self._write_rowcount(
            "UPDATE prompt_send_ledger SET user_row_id = ?, updated_at = ? "
            "WHERE session_key = ? AND client_send_id = ? AND holder = ? AND state = ?",
            (int(user_row_id), time.time(), session_key, send_id, holder, _IN_PROGRESS)))

    def bind_accepted_prompt_send_row(self, session_key: str, send_id: str, user_row_id: int) -> bool:
        """Stamp the user row a send wrote AFTER it was accepted (busy-queue path).

        The busy path accepts before it consumes images or queues anything, so its
        accept-time row lands after the settle. Binding it here lets a retry on another
        runtime prove the message is already in the transcript. First binding wins."""
        if not session_key or not send_id or user_row_id is None:
            return False
        return bool(self._write_rowcount(
            "UPDATE prompt_send_ledger SET user_row_id = ?, updated_at = ? "
            "WHERE session_key = ? AND client_send_id = ? AND state = ? AND user_row_id IS NULL",
            (int(user_row_id), time.time(), session_key, send_id, _ACCEPTED)))

    def accept_prompt_send(
        self, session_key: str, send_id: str, result: Optional[Dict[str, Any]], *, holder: str,
    ) -> bool:
        """Settle the reservation: from here every retry of this id replays ``result``."""
        if not session_key or not send_id:
            return False
        try:
            encoded = json.dumps(result if isinstance(result, dict) else {}, ensure_ascii=False)
        except (TypeError, ValueError):
            logger.warning("prompt send ledger: unserialisable submit result; storing a bare acceptance")
            encoded = "{}"
        return bool(self._write_rowcount(
            "UPDATE prompt_send_ledger SET state = ?, result_json = ?, holder = '', "
            "expires_at = 0, updated_at = ? WHERE session_key = ? AND client_send_id = ? "
            "AND holder = ? AND state = ?",
            (_ACCEPTED, encoded, time.time(), session_key, send_id, holder, _IN_PROGRESS)))

    def release_prompt_send(self, session_key: str, send_id: str, *, holder: str) -> bool:
        """Give an unfinished reservation back after a submit that was refused or failed.

        Nothing durable landed -> the row goes, and the id is as good as new (the fence
        refusing a send must never make that message unsendable). A durable user row DID
        land -> the row stays as ``failed`` carrying it, so the retry runs the turn the
        user never got while adopting the message already in their transcript."""
        if not session_key or not send_id:
            return False

        def _do(conn):
            row = conn.execute(
                "SELECT user_row_id FROM prompt_send_ledger WHERE session_key = ? "
                "AND client_send_id = ? AND holder = ? AND state = ?",
                (session_key, send_id, holder, _IN_PROGRESS)).fetchone()
            if row is None:
                return False  # accepted, or taken over by someone else: not ours to release
            if row[0] is None:
                conn.execute(
                    "DELETE FROM prompt_send_ledger WHERE session_key = ? AND client_send_id = ?",
                    (session_key, send_id))
            else:
                conn.execute(
                    "UPDATE prompt_send_ledger SET state = ?, holder = '', expires_at = 0, "
                    "updated_at = ? WHERE session_key = ? AND client_send_id = ?",
                    (_FAILED, time.time(), session_key, send_id))
            return True

        return bool(self._execute_write(_do))

    def prompt_send_ledger_entry(
        self, session_key: str, send_id: str) -> Optional[Dict[str, Any]]:
        """One ledger row as a dict, or None. Diagnostics and tests; the write paths read
        their own row inside their transaction."""
        row = self._read_one(_LEDGER_ROW_SQL, (session_key, send_id))
        if row is None:
            return None
        return {
            "payload_digest": row[0], "state": row[1], "holder": row[2], "user_row_id": row[3],
            "result": _decoded_result(row[4]), "expires_at": row[5]}

    def _prune_prompt_send_ledger_on_conn(
        self, conn, session_key: str, *, keep: int, max_age_s: float, now: float) -> int:
        """Retention inside the caller's transaction: drop rows for this chat that are BOTH
        older than ``max_age_s`` and outside the newest ``keep``."""
        cursor = conn.execute(
            "DELETE FROM prompt_send_ledger WHERE session_key = ? AND updated_at < ? "
            "AND client_send_id NOT IN (SELECT client_send_id FROM prompt_send_ledger "
            "WHERE session_key = ? ORDER BY updated_at DESC, rowid DESC LIMIT ?)",
            (session_key, now - max(0.0, float(max_age_s)), session_key, max(0, int(keep))))
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    def prune_prompt_send_ledger(
        self, session_key: str, *, keep: int = PROMPT_SEND_LEDGER_KEEP,
        max_age_s: float = PROMPT_SEND_LEDGER_MAX_AGE_S,
    ) -> int:
        """Standalone retention sweep for one chat; ``reserve_prompt_send`` runs the same
        prune inside its own transaction on every genuinely new send."""
        if not session_key:
            return 0
        now = time.time()
        return int(self._execute_write(lambda conn: self._prune_prompt_send_ledger_on_conn(
            conn, session_key, keep=keep, max_age_s=max_age_s, now=now)))

    def append_prompt_send_user_message(
        self, session_id: str, *, send_id: str, holder: str, content: str,
        display_kind: Optional[str] = None, timestamp: Any = None,
        display_metadata: Optional[Dict[str, Any]] = None, message_uid: Optional[str] = None,
        ledger_key: Optional[str] = None,
    ) -> int:
        """Append a submit-time user row and bind it to its reservation atomically.

        A committed transcript row without ``prompt_send_ledger.user_row_id`` cannot be
        recognised after a crash and would be appended again by the retry. Both writes
        share one transaction; a lost reservation rolls the insert and counter bump back.
        ``ledger_key`` is the STORED session id the reservation is keyed by when the row
        itself lands under a rotated agent session id (defaults to ``session_id``).
        """
        ledger_key = ledger_key or session_id
        if not session_id or not send_id or not holder:
            raise ValueError("prompt-send message append requires session, send and holder ids")
        from agent.message_metadata import stamp_message_uid
        from hermes_state_messages import _INSERT_MESSAGE_SQL, _coerce_timestamp
        message_timestamp = _coerce_timestamp(timestamp, time.time())
        msg = {"content": content, "display_kind": display_kind,
               "display_metadata": display_metadata,
               **({"message_uid": message_uid} if message_uid else {})}
        stamp_message_uid(msg)
        params = self._message_row_params(
            session_id, "user", msg, None, message_timestamp, keep_reasoning=True)

        def _do(conn):
            ledger = conn.execute(
                "SELECT state, holder, user_row_id FROM prompt_send_ledger "
                "WHERE session_key = ? AND client_send_id = ?",
                (ledger_key, send_id),
            ).fetchone()
            if ledger is None or ledger[0] != "in_progress" or ledger[1] != holder:
                raise RuntimeError("prompt-send reservation was lost before transcript append")
            if ledger[2] is not None:
                existing = conn.execute(
                    "SELECT role FROM messages WHERE id = ? AND active = 1", (int(ledger[2]),),
                ).fetchone()
                if existing is not None and existing[0] == "user":
                    return int(ledger[2])
            self._check_transcript_write_guards(conn, session_id, None)
            row_id = conn.execute(_INSERT_MESSAGE_SQL, params).lastrowid
            self._bump_session_counters(conn, session_id, 1, 0, unit=True)
            changed = conn.execute(
                "UPDATE prompt_send_ledger SET user_row_id = ?, updated_at = ? "
                "WHERE session_key = ? AND client_send_id = ? AND holder = ? "
                "AND state = 'in_progress' AND user_row_id IS NULL",
                (row_id, time.time(), ledger_key, send_id, holder),
            ).rowcount
            if changed != 1:
                raise RuntimeError("prompt-send row binding was not applied")
            return int(row_id)

        return self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)
