"""Cross-process active chat session leases.

The session database records persisted conversations; this module records
currently open chat surfaces, including idle CLI/TUI sessions that have not
written a transcript row yet.
"""

from __future__ import annotations

import json
import logging
import collections
import errno
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from hermes_constants import get_default_hermes_root, get_hermes_home, named_profile_is_live
from utils import atomic_json_write

logger = logging.getLogger(__name__)


class ActiveSessionRegistryError(RuntimeError):
    """The liveness registry could not prove a safe ownership decision."""


def coerce_max_concurrent_sessions(value: Any, key: str = "max_concurrent_sessions") -> Optional[int]:
    """Return a positive integer cap, or None when disabled/invalid."""
    if value is None:
        return None
    try:
        if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
            raise ValueError(value)
        parsed = int(value.strip(), 10) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring invalid %s=%r (expected a positive integer; 0/null disables)", key, value
        )
        return None
    return parsed if parsed > 0 else None


def resolve_max_concurrent_sessions(config: Any) -> Optional[int]:
    """Resolve top-level max_concurrent_sessions with gateway.* fallback."""
    raw: Any = None
    key = "max_concurrent_sessions"
    if isinstance(config, dict):
        if "max_concurrent_sessions" in config:
            raw = config.get("max_concurrent_sessions")
        else:
            gateway_cfg = config.get("gateway")
            if isinstance(gateway_cfg, dict):
                raw = gateway_cfg.get("max_concurrent_sessions")
                key = "gateway.max_concurrent_sessions"
    else:
        raw = getattr(config, "max_concurrent_sessions", None)
    return coerce_max_concurrent_sessions(raw, key=key)


def format_age(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h" if not minutes else f"{hours}h{minutes}m"


def summarize_holders(entries: list[dict[str, Any]]) -> str:
    """Compact "who is holding the slots" phrase, e.g. ``desktop x4, cli``."""
    if not entries:
        return ""
    counts = collections.Counter(str(e.get("surface") or "unknown") for e in entries)
    held = ", ".join(
        f"{surface} x{n}" if n > 1 else surface
        for surface, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    started = [t for t in (_optional_float(e.get("started_at")) for e in entries) if t]
    if started:
        held += f", oldest {format_age(time.time() - min(started))} ago"
    return held


def active_session_limit_message(
    active_count: int, max_sessions: int, entries: Optional[list[dict[str, Any]]] = None
) -> str:
    # Name the holders: slots are shared across CLI, desktop/TUI and gateway,
    # so the rejected surface is usually NOT the one squatting on them.
    held = summarize_holders(entries or [])
    detail = f" Held by: {held}." if held else ""
    return (
        f"Hermes is at the active session limit ({active_count}/{max_sessions})."
        f"{detail} Try again when another session finishes."
    )


# Machine-readable refusal reasons (the reason is the contract, the message is for
# people). Capacity = "busy, come back later"; ownership = "a live owner exists and
# writing would interleave with theirs".
SESSION_NOT_OWNED = "SESSION_NOT_OWNED"
MAX_CONCURRENT_SESSIONS = "MAX_CONCURRENT_SESSIONS"
# Ownership could not be PROVEN (registry unreadable/corrupt). Deliberately distinct
# from SESSION_NOT_OWNED: treating "can't tell" as a go-ahead is the fail-open hole
# that let two writers share one session.
# Distinct from SESSION_NOT_OWNED on purpose -- "someone else owns this" and "I cannot tell who owns this"
# call for different operator action, and collapsing the second into a silent go-ahead is exactly the
# fail-open hole that let two writers share one session (#94595 review, blocker 2).
SESSION_COORDINATION_UNAVAILABLE = "SESSION_COORDINATION_UNAVAILABLE"
# Handoff refusals. The claimant's observation no longer describes the live owner
# (another device claimed first, the owner rotated, the claim was replayed), or the
# owner is mid-turn. Both are RETRYABLE by re-observing — unlike SESSION_NOT_OWNED,
# they say "your evidence is out of date", not "you may never write here".
SESSION_HANDOFF_STALE = "SESSION_HANDOFF_STALE"
SESSION_HANDOFF_BUSY = "SESSION_HANDOFF_BUSY"

# Advertised through the gateway. A module constant, not a config flag: it holds
# because try_acquire_active_session checks atomically, so it cannot drift from the
# enforcement without this file changing.
PER_SESSION_EXCLUSIVE_SUBMIT = True


class ActiveSessionRefusal(str):
    """Refusal message (a ``str``, so callers are untouched) with a machine-readable ``reason``.

    ``ownership`` is the live owner's handoff precondition (``ActiveSessionOwner.as_wire``)
    when one was observed while refusing — what a claimant needs to offer "move it here".
    """

    reason: str
    ownership: Optional[dict[str, Any]]

    def __new__(
        cls, message: str, reason: str, ownership: Optional[dict[str, Any]] = None
    ) -> "ActiveSessionRefusal":
        obj = super().__new__(cls, message)
        obj.reason = reason
        obj.ownership = ownership
        return obj

    def __getnewargs_ex__(self) -> tuple[tuple[str, str], dict[str, Any]]:
        # copy/pickle rebuild via __new__: without the reason (and ownership), publishing a refused turn's
        # message.complete (deepcopy of the payload) raises and the viewer hangs.
        return (str(self), self.reason), {"ownership": self.ownership}


def format_refusal_stderr(message: str) -> str:
    """Keep the refusal contract across the one-shot CLI subprocess boundary."""
    reason = getattr(message, "reason", "")
    return f"hermes-refusal-reason: {reason}\n{message}" if reason else str(message)


def _is_same_writer(entry: dict[str, Any], metadata: Optional[dict[str, Any]]) -> bool:
    """True when an existing lease belongs to the very caller re-acquiring it.
    Identity is (pid, live_session_id): pid alone lets two live sessions in one process
    steal each other's lease; the live id alone lets another process with an equal id."""
    try:
        if int(entry.get("pid") or -1) != os.getpid():
            return False
    except (TypeError, ValueError):
        return False
    existing_live = str((entry.get("metadata") or {}).get("live_session_id") or "")
    incoming_live = str((metadata or {}).get("live_session_id") or "")
    return bool(existing_live and incoming_live) and existing_live == incoming_live


def session_owner_details(session_id: str, entry: dict[str, Any]) -> str:
    """The ``Details:`` line shared by every owner-refusal message."""
    surface = str(entry.get("surface") or "another surface")
    started = _optional_float(entry.get("started_at"))
    age = f" {format_age(time.time() - started)} ago" if started else ""
    return f"Details: session {session_id} opened by {surface}{age}."


def session_already_owned_message(session_id: str, entry: dict[str, Any]) -> str:
    """Refusal text for a session another live process holds.

    Contract shared with the TUI/Desktop surfaces: the FIRST line is the plain user sentence
    (no lease/pid/owner jargon); the second line is ``Details: ...`` for logs and bug reports.
    """
    return (
        "This chat is open in another Hermes window/terminal. Use it there, or start a new chat here.\n"
        + session_owner_details(session_id, entry)
    )


def _registry_home(registry_home: str | Path | None = None) -> Path:
    return Path(registry_home) if registry_home is not None else Path(get_hermes_home())


def _state_path(registry_home: str | Path | None = None) -> Path:
    return _registry_home(registry_home) / "runtime" / "active_sessions.json"


def _lock_path(registry_home: str | Path | None = None) -> Path:
    return _registry_home(registry_home) / "runtime" / "active_sessions.lock"


def _lease_paths(
    lease: Optional[ActiveSessionLease] = None, registry_home: str | Path | None = None
) -> tuple[Path, Path]:
    if lease is not None and lease.state_path is not None and lease.lock_path is not None:
        return lease.state_path, lease.lock_path
    home = _registry_home(registry_home)
    return home / "runtime" / "active_sessions.json", home / "runtime" / "active_sessions.lock"


def _flock(fh, *, lock: bool, timeout: Optional[float] = None) -> None:
    """Exclusive whole-file lock/unlock on ``fh`` (fcntl on POSIX, msvcrt on Windows). With ``timeout``
    the lock is retried only while another holder has it, then BlockingIOError is raised, so a holder
    stopped mid-write (SIGSTOP, a debugger) cannot stall the caller; other errors raise at once."""
    if lock and timeout is not None:
        from pm.filesystem import lock_fd
        if not lock_fd(fh.fileno(), wait=True, timeout=timeout):
            raise BlockingIOError(errno.EWOULDBLOCK, f"file lock still held after {timeout}s")
        return
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK if lock else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX if lock else fcntl.LOCK_UN)


class _FileLock:
    def __init__(self, path: Path, *, timeout: Optional[float] = None):
        self.path = path
        self.timeout = timeout
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        try:
            _flock(self._fh, lock=True, timeout=self.timeout)
        except Exception as exc:
            self._fh.close()
            self._fh = None
            raise RuntimeError(f"file lock unavailable: {self.path}") from exc
        return self

    def __exit__(self, exc_type, exc, tb):
        fh, self._fh = self._fh, None
        if fh is not None:
            with suppress(Exception):
                _flock(fh, lock=False)
            fh.close()


def _read_entries(path: Path, *, strict: bool = False) -> list[dict[str, Any]]:
    def invalid(what: str) -> ActiveSessionRegistryError:
        return ActiveSessionRegistryError(f"active session registry {what}: {path}")

    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except Exception as exc:
        if strict:
            raise invalid("unreadable") from exc
        logger.warning("Ignoring corrupt active session registry at %s", path)
        return []
    entries = data.get("entries") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        if strict:
            raise invalid("has invalid shape")
        return []
    valid = [entry for entry in entries if isinstance(entry, dict)]
    if not strict:
        return _without_queued_releases(path, valid)
    if len(valid) != len(entries):
        raise invalid("contains invalid entries")
    seen_leases: set[str] = set()
    for entry in valid:
        lease_id = entry.get("lease_id")
        # (problem-if-True predicate, message fragment) — checked lazily, in
        # this order, so an unhashable lease id is reported before the dup check.
        for bad, what in (
            (lambda: not _nonblank_str(lease_id), "an invalid lease id"),
            (lambda: lease_id in seen_leases, "a duplicate lease id"),
            (lambda: not _nonblank_str(entry.get("session_id")), "an invalid session id"),
            (lambda: _registry_pid(entry.get("pid")) <= 0, "an invalid pid"),
            (lambda: not _optional_isinstance(entry.get("surface"), str), "an invalid surface"),
            (lambda: not _optional_isinstance(entry.get("track_liveness"), bool), "an invalid liveness marker"),
            (lambda: not _optional_isinstance(entry.get("metadata"), dict), "invalid metadata"),
            (lambda: not _valid_process_start(entry.get("process_start_time")), "an invalid process start time"),
            (lambda: not _valid_generation(entry.get("generation")), "an invalid fencing generation"),
            (lambda: not _optional_isinstance(entry.get("busy_turn"), str), "an invalid turn marker"),
        ):
            if bad():
                raise invalid(f"contains {what}")
        seen_leases.add(lease_id)
    return _without_queued_releases(path, valid)


# Leases this process has queued for release (gateway/active_lease_io.py) whose durable drop has not
# landed yet, keyed by registry file. Every in-process read hides them, so a claim, count or
# ownership check made right after a turn end sees the registry as if the release had already run
# (a slot freed, the session unowned); the next locked write persists the drop. Other processes
# still see the entry until the release worker rewrites the file.
_queued_releases: dict[str, set[str]] = {}
_queued_releases_lock = threading.Lock()


def _registry_key(path: Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def mark_release_queued(lease: ActiveSessionLease) -> None:
    """Retire ``lease`` from this process's view of the registry now; its file write comes later."""
    if not isinstance(lease, ActiveSessionLease) or not lease.enabled or lease.released:
        return
    state_path, _ = _lease_paths(lease)
    with _queued_releases_lock:
        _queued_releases.setdefault(_registry_key(state_path), set()).add(lease.lease_id)


def _unmark_release_queued(state_path: Path, lease_id: str) -> None:
    """Make ``lease_id`` visible again to the caller holding the registry lock, which drops it."""
    key = _registry_key(state_path)
    with _queued_releases_lock:
        queued = _queued_releases.get(key)
        if queued is not None:
            queued.discard(lease_id)
            if not queued:
                del _queued_releases[key]


def _without_queued_releases(path: Path, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not _queued_releases:
        return entries
    with _queued_releases_lock:
        queued = set(_queued_releases.get(_registry_key(path), ()))
    if not queued:
        return entries
    return [e for e in entries if str(e.get("lease_id") or "") not in queued]


def _nonblank_str(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _optional_isinstance(v: Any, typ) -> bool:
    return v is None or isinstance(v, typ)


def _registry_pid(pid: Any) -> int:
    """Registry pid as int; 0 for bools, non-int/str, or unparseable values."""
    if isinstance(pid, bool) or not isinstance(pid, (int, str)):
        return 0
    try:
        return int(pid)
    except (TypeError, ValueError):
        return 0


def _valid_process_start(v: Any) -> bool:
    if v in (None, ""):
        return True
    parsed = _optional_float(v)
    return parsed is not None and math.isfinite(parsed)


def _finite_generation(v: Any) -> int:
    """``v`` as a finite positive generation, else 0. Bools, floats, strings, NaN and
    out-of-range values are all "not a generation" — a fencing comparison against
    anything but a plain positive int is not a comparison."""
    if not isinstance(v, int) or isinstance(v, bool):
        return 0
    return v if 0 < v <= _MAX_GENERATION else 0


# A generation is a hand-counted human action (one per explicit handoff), so any value
# near this bound is corruption, not history. Bounding it keeps the CAS total-ordered
# and the JSON small.
_MAX_GENERATION = 1 << 40


def _valid_generation(v: Any) -> bool:
    """Absent (written by a Hermes without fencing) or a finite positive int."""
    return v is None or _finite_generation(v) > 0


def _entry_generation(entry: dict[str, Any]) -> int:
    """The entry's fencing generation, or 0 when its writer does not speak the protocol."""
    return _finite_generation(entry.get("generation"))


# ``session_id -> [generation, updated_at]`` high-water marks, persisted beside the
# entries. Written only when a session is HANDED OFF, so the map holds one small record
# per chat a user has ever moved between devices — never one per chat.
#
# Its single job is monotonicity: a lease acquired after a handoff must not be issued a
# generation the fence already passed, so the CAS precondition stays totally ordered.
# The record must outlive every live owner and every unrelated handoff: transfer may
# legitimately recreate an absent entry after runtime-id rotation, and this permanent
# high-water mark is what distinguishes that recovery from a superseded lease trying to
# resurrect itself.
_FENCE_KEY = "fences"


def _read_fences(path: Path) -> dict[str, list]:
    """The high-water map, tolerantly: anything unparseable reads as "no history"."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):  # missing/unreadable file or corrupt JSON: no history
        return {}
    fences = data.get(_FENCE_KEY) if isinstance(data, dict) else None
    if not isinstance(fences, dict):
        return {}
    return {
        str(key): [_finite_generation(value[0]), _optional_float(value[1]) or 0.0]
        for key, value in fences.items()
        if isinstance(value, (list, tuple)) and len(value) == 2 and _finite_generation(value[0]) > 0
    }


def _fenced_generation(fences: dict[str, list], session_id: str) -> int:
    record = fences.get(str(session_id))
    return _finite_generation(record[0]) if isinstance(record, (list, tuple)) and record else 0


def _bump_fence(fences: dict[str, list], session_id: str, generation: int) -> dict[str, list]:
    """Record ``generation`` as the high-water mark for ``session_id`` (monotonic)."""
    key = str(session_id)
    updated = dict(fences)
    updated[key] = [max(generation, _fenced_generation(fences, key)), time.time()]
    return updated


def _next_generation(path: Path, session_id: str) -> int:
    """The generation a FRESH lease on ``session_id`` must take: past every fence issued."""
    return _fenced_generation(_read_fences(path), session_id) + 1


def _entry_busy(entry: dict[str, Any]) -> bool:
    """Whether the owner has a turn in flight. Set by the owner under THIS file's lock, so an
    idle reading is a fact about the owner's last mutation boundary, not a guess from a clock."""
    return bool(str(entry.get("busy_turn") or "").strip())


def _write_entries(
    path: Path, entries: list[dict[str, Any]], fences: Optional[dict[str, list]] = None
) -> None:
    """Persist the live entries. ``fences`` defaults to whatever is already on disk, so the
    high-water marks survive every writer that has no reason to know about them."""
    resolved = _read_fences(path) if fences is None else fences
    payload: dict[str, Any] = {"entries": entries}
    if resolved:
        payload[_FENCE_KEY] = resolved
    atomic_json_write(path, payload, indent=None, sort_keys=True)


def _process_start_time(pid: int) -> Optional[float]:
    # Pair pid with create_time when psutil can read it, so a recycled pid does not
    # keep a stale lease alive indefinitely.
    try:
        import psutil  # type: ignore
        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


_OWN_START: tuple[int, float] | None = None  # (pid, create_time); published atomically, re-read after fork


def _own_start_time() -> Optional[float]:
    """This process's create_time, read from psutil once instead of per lease probe."""
    global _OWN_START
    pid = os.getpid()
    if _OWN_START is None or _OWN_START[0] != pid:
        start = _process_start_time(pid)
        if start is None:
            return None
        _OWN_START = (pid, start)
    return _OWN_START[1]


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pid_liveness(pid: Any, process_start_time: Any = None, *, lenient: bool = False) -> Optional[bool]:
    """True/False for live/dead, or None when unknowable. ``lenient`` never returns None:
    an unparseable pid or failed existence probe counts as dead, an unreadable start as alive."""
    unknown_dead = False if lenient else None
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        pid_int = 0
    if pid_int <= 0:
        return unknown_dead
    is_self = pid_int == os.getpid()  # trivially exists; the (pid, start) identity check still applies
    if not is_self:
        try:
            from gateway.status import _pid_exists
            if not _pid_exists(pid_int):
                return False
        except Exception:
            return unknown_dead
    expected_start = _optional_float(process_start_time)
    if expected_start is None:
        return True
    current_start = _own_start_time() if is_self else _process_start_time(pid_int)
    if current_start is None:
        return True if lenient else None
    return abs(current_start - expected_start) < 0.001


def _prune_dead(
    entries: list[dict[str, Any]], *, strict: bool = False,
    target_session_id: str | None = None, target_pid: int | None = None,
) -> list[dict[str, Any]]:
    """Keep entries whose owner is alive; tracked/strict entries must be provably so.

    With ``target_session_id`` only THAT session's owner has to be provable: an
    unrelated sibling whose liveness is unknowable (pid present, start time
    unreadable — an LXC ``/proc`` after a backend restart) stays in the live set,
    so it still fences its own session and still counts toward capacity, but no
    longer refuses every claim/release for a different session id. See #113683.
    ``target_pid`` scopes the same way by owner pid (the orphan sweep only ever
    reclaims this process's own leases).
    """
    targeted = target_session_id is not None or target_pid is not None
    live: list[dict[str, Any]] = []
    for entry in entries:
        tracked = strict or bool(entry.get("track_liveness"))
        state = _pid_liveness(
            entry.get("pid"), entry.get("process_start_time"), lenient=not tracked
        )
        if state is None:
            if (
                not targeted
                or (target_session_id is not None
                    and str(entry.get("session_id") or "") == str(target_session_id))
                or (target_pid is not None and entry.get("pid") == target_pid)
            ):
                raise ActiveSessionRegistryError("active session owner liveness is unknown")
            state = True
        if state:
            live.append(entry)
    return live


@dataclass
class ActiveSessionLease:
    lease_id: str
    session_id: str
    surface: str
    enabled: bool = True
    released: bool = False
    # Fencing generation of the registry slot this lease was written with. A handoff
    # advances it, which is what makes the previous owner's still-referenced lease
    # object stop validating (it cannot be reached and deleted in another process).
    # 0 = pre-fencing lease / inert token: validation then keys on lease_id alone.
    generation: int = 0
    # Writer identity (``live_session_id``) as recorded in the registry, so a
    # re-assertion after a transient prune restores the same identity.
    metadata: Optional[dict[str, Any]] = None
    # Pinned at acquisition: a lease taken under the root HERMES_HOME must release
    # against the same registry even inside a profile-home override, or phantom
    # leases fill the session cap.
    # See #85431.
    state_path: Optional[Path] = None
    lock_path: Optional[Path] = None
    track_liveness: bool = False

    def release(self) -> None:
        if self.released or not self.enabled:
            return
        release_active_session(self)


def _clean_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {str(k): v for k, v in metadata.items() if isinstance(k, str)}


def _drop_lease(
    state_path: Path, entries: list[dict[str, Any]], lease_id: str
) -> list[dict[str, Any]]:
    """Remove ``lease_id`` from ``entries``, writing the registry only if it was present."""
    kept = [e for e in entries if str(e.get("lease_id") or "") != lease_id]
    if len(kept) != len(entries):
        _write_entries(state_path, kept)
    return kept


def _holds_session(entries: list[dict[str, Any]], session_id: str) -> bool:
    target = str(session_id or "")
    return bool(target) and any(str(e.get("session_id") or "") == target for e in entries)


def _read_live_entries(
    state_path: Path, *, track_liveness: bool, warn: str,
    target_session_id: str | None = None, target_pid: int | None = None,
) -> Optional[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    """``(raw, pruned)`` from the registry, or None when it is unreadable.

    Liveness-tracked callers re-raise instead (they must not proceed on an unprovable
    registry); untracked callers get ``warn`` logged and decide how to degrade.
    ``target_session_id`` is the session the caller is about to claim/release (see
    ``_prune_dead``).
    """
    try:
        raw_entries = _read_entries(state_path, strict=True)
        return raw_entries, _prune_dead(
            raw_entries, strict=track_liveness,
            target_session_id=target_session_id, target_pid=target_pid,
        )
    except ActiveSessionRegistryError:
        if track_liveness:
            raise
        logger.warning(warn)
        return None


def _lease_entry(
    *, lease_id: str, session_id: str, surface: str,
    metadata: Optional[dict[str, Any]] = None, track_liveness: bool = False,
    generation: int = 1,
) -> dict[str, Any]:
    now = time.time()
    entry: dict[str, Any] = {
        "lease_id": lease_id,
        "session_id": str(session_id),
        "surface": str(surface),
        "pid": os.getpid(),
        "process_start_time": _own_start_time(),
        "started_at": now,
        "updated_at": now,
        "generation": max(1, int(generation)),
    }
    if track_liveness:
        entry["track_liveness"] = True
    if metadata:
        entry["metadata"] = _clean_metadata(metadata)
    return entry


def try_acquire_active_session(
    *, session_id: str, surface: str, config: Any, metadata: Optional[dict[str, Any]] = None,
    registry_home: str | Path | None = None, track_liveness: bool = False,
) -> tuple[Optional[ActiveSessionLease], Optional[str]]:
    """Acquire an active-session slot: ``(lease, None)`` or ``(None, ActiveSessionRefusal)``.

    Per-session exclusivity is CORRECTNESS, enforced unconditionally (at most one live
    owner per stored session); ``max_concurrent_sessions`` is resource POLICY, applied
    only when configured. ``registry_home`` lets profile-scoped backends share the owning
    profile's registry. Ownership uncertainty fails CLOSED (SESSION_COORDINATION_UNAVAILABLE).

    Liveness tracking keeps richer desktop lifecycle semantics; ``registry_home`` lets profile-scoped
    backends share the owning profile's registry even when launched from another home. See #94595.
    """
    max_sessions = resolve_max_concurrent_sessions(config)
    lease_id = uuid.uuid4().hex
    key = str(session_id or "")

    # No stored id yet => nothing to fence or record (and the strict schema
    # refuses empty session ids): hand back a no-op lease.
    if not key and not track_liveness:
        return ActiveSessionLease(
            lease_id=lease_id, session_id=key, surface=str(surface), enabled=False
        ), None

    state_path, lock_path = _lease_paths(registry_home=registry_home)
    lease = ActiveSessionLease(
        lease_id=lease_id, session_id=key, surface=str(surface), state_path=state_path,
        lock_path=lock_path, track_liveness=track_liveness,
        metadata=_clean_metadata(metadata) if metadata else None,
    )
    with _FileLock(lock_path):
        # Past every generation this session has ever been fenced at, so a lease minted
        # after a handoff can never be confused with the superseded one.
        entry = _lease_entry(
            lease_id=lease_id, session_id=key, surface=str(surface), metadata=metadata,
            track_liveness=track_liveness, generation=_next_generation(state_path, key),
        )
        lease.generation = _entry_generation(entry)
        # A capacity cap could degrade open; exclusivity cannot: "could not
        # prove ownership" must never become "no owner exists".
        loaded = _read_live_entries(
            state_path, track_liveness=track_liveness,
            warn="Active-session registry is unavailable; refusing the session "
                 "rather than risking a concurrent writer",
            target_session_id=key,
        )
        if loaded is None:
            return None, ActiveSessionRefusal(
                "Hermes could not read the active-session registry at "
                f"{state_path}, so it cannot prove this session has no other "
                "live owner. Fix or remove that file and try again.",
                SESSION_COORDINATION_UNAVAILABLE,
            )
        raw_entries, entries = loaded
        pruned = len(raw_entries) - len(entries)
        if pruned:
            logger.info("Pruned %d stale active session lease(s)", pruned)

        def refuse(
            message: str, reason: str, log: str, *args, ownership: Optional[dict[str, Any]] = None,
        ) -> tuple[None, ActiveSessionRefusal]:
            _write_entries(state_path, entries)  # persist the prune even when refusing
            logger.info(log, *args)
            return None, ActiveSessionRefusal(message, reason, ownership)

        # Correctness first, under the same lock that just pruned dead owners.
        # An empty key is exempt: treating "" as an identity would make every
        # unsaved draft exclude every other one.
        if key:
            for index, existing in enumerate(entries):
                if str(existing.get("session_id") or "") != key:
                    continue
                # The same writer is not a second writer: a live session that
                # leaked its lease reference would otherwise be fenced out of
                # its own session permanently (pruning only removes entries
                # whose PROCESS is dead). Re-entrancy, not concurrency.
                if _is_same_writer(existing, metadata):
                    # Same writer, same slot: keep the fencing generation (and any turn
                    # marker) it already had — re-entrancy is not a fence event, and
                    # bumping here would invalidate a claimant's fresh observation.
                    entry["generation"] = _entry_generation(existing) or entry["generation"]
                    if _entry_busy(existing):
                        entry["busy_turn"] = existing["busy_turn"]
                    entries[index] = entry
                    lease.generation = _entry_generation(entry)
                    _write_entries(state_path, entries)
                    return lease, None
                return refuse(
                    session_already_owned_message(key, existing), SESSION_NOT_OWNED,
                    "Refused active session %s: already held by pid=%s surface=%s",
                    key, existing.get("pid"), existing.get("surface"),
                    ownership=_owner_from_entry(existing).as_wire(),
                )

        # Capacity second, and only when an operator asked for one.
        if max_sessions is not None and len(entries) >= max_sessions:
            return refuse(
                active_session_limit_message(len(entries), max_sessions, entries),
                MAX_CONCURRENT_SESSIONS,
                "Active session limit reached: active=%d max=%d surface=%s",
                len(entries), max_sessions, surface,
            )
        entries.append(entry)
        _write_entries(state_path, entries)

    return lease, None


# ── explicit handoff: fencing generation + compare-and-swap ────────────────────
#
# Exclusivity is a fence, and a fence needs a gate. The gate is a USER-INVOKED
# handoff, never an implicit takeover on resume/open, and it rests on three
# properties this section implements:
#
#   1. Exactness. The claimant names the (lease_id, generation) pair it observed.
#      Anything else -- a replay, a cached refusal, a second device that lost the
#      race -- fails the compare-and-swap and changes nothing.
#   2. Quiescence. The owner records its own turn boundaries in its registry entry
#      under this file's lock, so another PROCESS can prove the owner is idle
#      without interrupting it or trusting a heartbeat. A busy owner is refused.
#   3. Fencing. A committed handoff advances the generation. The old runtime's
#      lease object still exists in its own process (nothing can reach in and
#      delete it), so it is REVALIDATION at every mutation boundary that stops it.
#
# Every refusal path leaves the registry exactly as found: "I cannot prove this is
# safe" must never be the thing that rewrites ownership.


@dataclass(frozen=True)
class ActiveSessionOwner:
    """The live owner of one stored session — a claimant's handoff precondition."""

    session_id: str
    lease_id: str
    generation: int
    surface: str
    busy: bool
    started_at: Optional[float] = None

    @property
    def handoff_supported(self) -> bool:
        """False for an owner written by a Hermes without fencing: there is no generation
        to advance, so its lease could not be fenced and a handoff must not be offered."""
        return self.generation > 0

    def as_wire(self) -> dict[str, Any]:
        """The closed payload the gateway forwards to a claimant. Deliberately excludes
        pid, process start time, profile home and lease metadata: a claimant needs the
        CAS precondition and enough context to ask the user, nothing about the host."""
        age = None if self.started_at is None else max(0.0, round(time.time() - self.started_at, 3))
        return {
            "session_id": self.session_id,
            "lease_id": self.lease_id,
            "generation": self.generation,
            "surface": self.surface,
            "busy": self.busy,
            "age_seconds": age,
            "handoff_supported": self.handoff_supported,
        }


def _owner_from_entry(entry: dict[str, Any]) -> ActiveSessionOwner:
    return ActiveSessionOwner(
        session_id=str(entry.get("session_id") or ""),
        lease_id=str(entry.get("lease_id") or ""),
        generation=_entry_generation(entry),
        surface=str(entry.get("surface") or "unknown"),
        busy=_entry_busy(entry),
        started_at=_optional_float(entry.get("started_at")),
    )


def _coordination_refusal(state_path: Path, exc: Exception) -> ActiveSessionRefusal:
    return ActiveSessionRefusal(
        f"Hermes could not read the active-session registry at {state_path}, so it cannot "
        f"prove who owns this session ({exc}). Fix or remove that file and try again.",
        SESSION_COORDINATION_UNAVAILABLE,
    )


def _live_entries_for(state_path: Path, session_id: str) -> list[dict[str, Any]]:
    """Strictly-parsed live entries, with ``session_id``'s owner proven live or nothing."""
    return _prune_dead(
        _read_entries(state_path, strict=True), strict=True, target_session_id=session_id,
    )


def observe_active_session_owner(
    session_id: str, *, registry_home: str | Path | None = None,
) -> Optional[ActiveSessionOwner]:
    """The live owner of ``session_id``, or None when nobody owns it.

    Raises ``ActiveSessionRegistryError`` when the registry cannot be parsed or the
    owner's liveness cannot be proven — an unprovable registry is never "unowned".
    """
    key = str(session_id or "")
    if not key:
        return None
    state_path, lock_path = _lease_paths(registry_home=registry_home)
    with _FileLock(lock_path):
        for entry in _live_entries_for(state_path, key):
            if str(entry.get("session_id") or "") == key:
                return _owner_from_entry(entry)
    return None


def _inert(lease: Any) -> bool:
    """True for leases that never represented a registry slot: the disabled no-op lease of an
    unsaved draft, the inert borrowed token of a compute-host child, an already-released lease.
    Their legacy contract is unchanged — validation is a no-op, not a refusal."""
    return (
        lease is None
        or not getattr(lease, "enabled", False)
        or getattr(lease, "released", False)
        or not str(getattr(lease, "session_id", "") or "")
    )


def _owned_slot_locked(
    lease: ActiveSessionLease, state_path: Path,
) -> tuple[Optional[tuple[list[dict[str, Any]], int]], Optional[ActiveSessionRefusal]]:
    """Under the registry lock: ``((entries, index), None)`` when ``lease`` is still the exact
    owner of its session, else ``(None, refusal)``. Caller holds the lock.

    A slot with NO entry is refused, never re-asserted. A lease object is evidence of a past
    claim only, and an absent entry cannot distinguish "my entry was transiently pruned" from
    "the user moved this chat away and the claimant has since closed it" — and writing the
    stale holder back would resurrect a writer whose transcript is a generation out of date.
    Refusal is not a lockout: the refused runtime drops the lease and its next turn takes the
    ordinary acquisition path, which mints a FRESH lease under this lock when nobody owns the
    chat, and is refused again when somebody does.
    """
    key = str(lease.session_id)
    try:
        entries = _live_entries_for(state_path, key)
    except ActiveSessionRegistryError as exc:
        return None, _coordination_refusal(state_path, exc)
    for index, entry in enumerate(entries):
        if str(entry.get("session_id") or "") != key:
            continue
        lease_generation = _finite_generation(getattr(lease, "generation", 0))
        if str(entry.get("lease_id") or "") != str(lease.lease_id) or (
            lease_generation and _entry_generation(entry) != lease_generation
        ):
            owner = _owner_from_entry(entry)
            return None, ActiveSessionRefusal(
                session_already_owned_message(key, entry), SESSION_NOT_OWNED, owner.as_wire())
        return (entries, index), None
    logger.info(
        "Refusing active session %s: this runtime's lease %s (generation %s) has no registry entry",
        key, lease.lease_id, getattr(lease, "generation", 0))
    return None, ActiveSessionRefusal(
        "This chat is no longer open on this device. Reopen it and send again.", SESSION_NOT_OWNED)


def validate_active_session_lease(lease: Any) -> Optional[ActiveSessionRefusal]:
    """Re-prove that ``lease`` is still the registry's exact owner; None when it is.

    THE fence. Called at every mutation boundary, because an attached lease object is only
    evidence of a PAST claim: after a handoff it names a generation the registry has moved
    past, and trusting it is how two writers end up interleaving one transcript.
    """
    if _inert(lease):
        return None
    state_path, lock_path = _lease_paths(lease)
    with _FileLock(lock_path):
        return _owned_slot_locked(lease, state_path)[1]


def begin_active_session_turn(lease: Any, *, turn_token: str) -> Optional[ActiveSessionRefusal]:
    """Fence-check ``lease`` and mark its slot busy in ONE atomic step; None when admitted.

    Atomicity is the point: a claimant's quiescence probe and this reservation contend for
    the same file lock, so a turn either reserves before the handoff commits (and the
    handoff is refused busy) or after it (and this is refused SESSION_NOT_OWNED). There is
    no ordering in which both proceed. Idempotent for the same ``turn_token``, so a turn may
    re-assert it at successive boundaries without double-counting.
    """
    if _inert(lease):
        return None
    token = str(turn_token or "").strip() or "turn"
    state_path, lock_path = _lease_paths(lease)
    with _FileLock(lock_path):
        found, refusal = _owned_slot_locked(lease, state_path)
        if refusal is not None:
            return refusal
        entries, index = found
        existing = str(entries[index].get("busy_turn") or "").strip()
        if existing and existing != token:
            return ActiveSessionRefusal(
                "This chat is still finishing another turn. Try again when it settles.",
                SESSION_HANDOFF_BUSY,
                _owner_from_entry(entries[index]).as_wire(),
            )
        entries[index]["busy_turn"] = token
        entries[index]["updated_at"] = time.time()
        _write_entries(state_path, entries)
    return None


def end_active_session_turn(lease: Any, *, turn_token: str) -> None:
    """Clear ``lease``'s turn marker. Best-effort and strictly scoped to (lease_id, token):
    a fenced runtime unwinding its interrupted turn must not report the NEW owner's live
    turn as idle. Never raises — a finished turn cannot be failed by bookkeeping."""
    if _inert(lease):
        return
    token = str(turn_token or "").strip() or "turn"
    key = str(lease.session_id)
    state_path, lock_path = _lease_paths(lease)
    try:
        with _FileLock(lock_path):
            # strict: a registry we cannot parse keeps its bytes AND its busy marker
            # (fail-closed), rather than being rewritten from a lossy read.
            entries = _read_entries(state_path, strict=True)
            changed = False
            for entry in entries:
                if (str(entry.get("session_id") or "") == key
                        and str(entry.get("lease_id") or "") == str(lease.lease_id)
                        and str(entry.get("busy_turn") or "") == token):
                    entry.pop("busy_turn", None)
                    entry["updated_at"] = time.time()
                    changed = True
            if changed:
                _write_entries(state_path, entries)
    except Exception:
        logger.debug("failed to clear active-session turn marker for %s", key, exc_info=True)


def handoff_active_session(
    *, session_id: str, surface: str, expected_lease_id: str, expected_generation: int,
    metadata: Optional[dict[str, Any]] = None, registry_home: str | Path | None = None,
    track_liveness: bool = False,
) -> tuple[Optional[ActiveSessionLease], Optional[ActiveSessionRefusal]]:
    """Move ``session_id``'s write lease to THIS process: ``(lease, None)`` or ``(None, refusal)``.

    An exact compare-and-swap against the observation the claimant acted on. Refuses
    ``SESSION_HANDOFF_STALE`` when the pair no longer describes the live owner (replay, a
    racing second claimant, an owner that rotated), ``SESSION_HANDOFF_BUSY`` when the owner
    has a turn in flight, ``SESSION_COORDINATION_UNAVAILABLE`` when ownership cannot be
    proven. Every refusal leaves the registry byte-for-byte as found.

    Capacity is deliberately not consulted: a handoff MOVES a slot the cap already counted.
    """
    key = str(session_id or "")
    expected_lease = str(expected_lease_id or "")
    # A bool, a float, a NaN or an out-of-range value is not a generation: refuse rather
    # than coerce, so the compare-and-swap is always an exact integer comparison.
    expected_gen = _finite_generation(expected_generation)
    state_path, lock_path = _lease_paths(registry_home=registry_home)

    def stale(log: str, *args) -> tuple[None, ActiveSessionRefusal]:
        logger.info("Refused session handoff for %s: " + log, key, *args)
        return None, ActiveSessionRefusal(
            "This chat moved to another window since Hermes checked. Reopen it and try again.",
            SESSION_HANDOFF_STALE,
        )

    if not key or not expected_lease or expected_gen <= 0:
        return stale("incomplete observation (lease=%r generation=%r)", expected_lease, expected_gen)

    with _FileLock(lock_path):
        try:
            entries = _live_entries_for(state_path, key)
        except ActiveSessionRegistryError as exc:
            return None, _coordination_refusal(state_path, exc)
        owner_index = next(
            (i for i, e in enumerate(entries) if str(e.get("session_id") or "") == key), None)
        if owner_index is None:
            return stale("no live owner to hand off")
        existing = entries[owner_index]
        if str(existing.get("lease_id") or "") != expected_lease:
            return stale("lease id changed since it was observed")
        if _entry_generation(existing) != expected_gen:
            return stale("generation moved to %s since it was observed", _entry_generation(existing))
        if _entry_busy(existing):
            logger.info("Refused session handoff for %s: owner has a turn in flight", key)
            return None, ActiveSessionRefusal(
                "That chat is mid-reply on the other device. Wait for it to finish, then move it here.",
                SESSION_HANDOFF_BUSY, _owner_from_entry(existing).as_wire(),
            )
        lease_id = uuid.uuid4().hex
        generation = expected_gen + 1
        entries[owner_index] = _lease_entry(
            lease_id=lease_id, session_id=key, surface=str(surface), metadata=metadata,
            track_liveness=track_liveness, generation=generation,
        )
        # The high-water mark outlives both leases AND the entry: after the claimant
        # closes the chat, a fresh acquisition still starts past this fence.
        _write_entries(
            state_path, entries, _bump_fence(_read_fences(state_path), key, generation))
    logger.info(
        "Session %s handed off to surface=%s pid=%s generation=%d (fencing lease %s)",
        key, surface, os.getpid(), expected_gen + 1, expected_lease)
    return ActiveSessionLease(
        lease_id=lease_id, session_id=key, surface=str(surface), state_path=state_path,
        lock_path=lock_path, track_liveness=track_liveness, generation=generation,
        metadata=_clean_metadata(metadata) if metadata else None,
    ), None


def release_active_session(lease: ActiveSessionLease) -> None:
    # Prefer the registry the lease was acquired against: the caller may be
    # running under a profile HERMES_HOME override.
    # See #85431.
    state_path, lock_path = _lease_paths(lease)
    with _FileLock(lock_path):
        _unmark_release_queued(state_path, lease.lease_id)
        if lease.released:
            return
        loaded = _read_live_entries(
            state_path, track_liveness=lease.track_liveness,
            warn="Active-session registry is unavailable; preserving it while "
                 "releasing an untracked lease",
            target_session_id=lease.session_id,
        )
        if loaded is not None:
            _drop_lease(state_path, loaded[1], lease.lease_id)
        lease.released = True


def transfer_active_session(
    lease: ActiveSessionLease, *, session_id: str, metadata: Optional[dict[str, Any]] = None
) -> bool:
    """Move an existing lease to a new session id without dropping the slot."""
    new_session_id = str(session_id or "")
    if not new_session_id or lease.released:
        return False
    if not lease.enabled:
        lease.session_id = new_session_id
        return True

    state_path, lock_path = _lease_paths(lease)
    with _FileLock(lock_path):
        # release() may have won after the optimistic precheck but before this
        # thread acquired the file lock. Never resurrect a durably removed lease.
        if lease.released:
            return False
        loaded = _read_live_entries(
            state_path, track_liveness=lease.track_liveness,
            warn="Active-session registry is unavailable; refusing to overwrite "
                 "it during lease transfer",
            target_session_id=lease.session_id,
        )
        if loaded is None:
            return False
        entries = loaded[1]
        lease_generation = _finite_generation(getattr(lease, "generation", 0))
        own = next((e for e in entries if str(e.get("lease_id") or "") == lease.lease_id), None)
        if own is not None:
            # Same lease id, different generation: the registry has moved past the
            # observation this lease object carries. Retargeting it would rename a
            # slot on behalf of a writer that no longer holds it.
            if lease_generation and _entry_generation(own) != lease_generation:
                return False
            own["session_id"] = new_session_id
            own["updated_at"] = time.time()
            if metadata:
                own["metadata"] = _clean_metadata(metadata)
        elif lease.track_liveness and _fenced_generation(
            _read_fences(state_path), lease.session_id
        ) <= lease_generation:
            # The upsert exists for an entry this process lost transiently (a pruned or
            # deleted registry), NOT for a superseded one: a fence above this lease's
            # generation is the durable record that the user moved the chat away, and it
            # outlives the claimant's entry. Recreating the slot from here would resurrect
            # a writer a generation behind the transcript. Refusal is not a lockout -- the
            # next turn holds no lease and takes the ordinary acquisition path, which mints
            # a fresh lease past the fence.
            entries.append(_lease_entry(
                lease_id=lease.lease_id, session_id=new_session_id, surface=lease.surface,
                metadata=metadata, track_liveness=True, generation=max(1, lease_generation),
            ))
        else:
            return False
        _write_entries(state_path, entries)
        lease.session_id = new_session_id
        return True


# A lease this process wrote in the last few seconds may not be in the caller's
# ``own_live_lease_ids`` yet: ``try_acquire_active_session`` writes the registry entry under
# the file lock and the server attaches the lease to its session record only after that
# returns. A concurrent finalize that snapshotted its live ids in between would otherwise
# read the brand-new lease as an orphan and drop it. Real orphans are minutes old.
# See #101415.
_SELF_ORPHAN_GRACE_SECONDS = 30.0


def _drop_self_orphans(
    entries: list[dict[str, Any]], own_live_lease_ids: set[str] | None
) -> list[dict[str, Any]]:
    """Drop this process's leases only when its caller can vouch for owners."""
    if own_live_lease_ids is None:
        return entries
    pid = os.getpid()
    cutoff = time.time() - _SELF_ORPHAN_GRACE_SECONDS
    return [
        entry for entry in entries
        if entry.get("pid") != pid
        or str(entry.get("lease_id") or "") in own_live_lease_ids
        or (_optional_float(entry.get("started_at")) or 0.0) > cutoff
    ]


def _release_orphaned_leases_in_home(registry_home: Path, live_lease_ids: set[str]) -> int:
    state_path = _state_path(registry_home)
    # No registry file yet means no leases have ever been written under this
    # home — don't take a lock (or create its file) on the idle-reaper tick.
    if not state_path.exists():
        return 0
    with _FileLock(_lock_path(registry_home)):
        loaded = _read_live_entries(
            state_path, track_liveness=False,
            warn="Active-session registry is unavailable; skipping orphaned-lease sweep",
            target_pid=os.getpid(),
        )
        if loaded is None:
            return 0
        entries = loaded[1]
        kept = _drop_self_orphans(entries, live_lease_ids)
        dropped = len(entries) - len(kept)
        if dropped:
            _write_entries(state_path, kept)
        return dropped


def release_orphaned_leases(live_lease_ids: set[str]) -> int:
    """Drop this process's registry entries that no live session owns.

    ``_prune_dead`` only reclaims leases of dead processes, so on a days-long server a
    lease whose session skipped teardown is held until restart. The owning process is the
    only authority on its own leases — exact, no heartbeat on the turn path, no threshold.
    Sweeps the root home and every profile home (a multiplexed server leases across them).
    """
    root = get_default_hermes_root()
    homes = [root]
    try:
        homes.extend(p for p in (root / "profiles").iterdir() if named_profile_is_live(p))
    except OSError:
        pass

    dropped = 0
    for home in homes:
        try:
            dropped += _release_orphaned_leases_in_home(home, live_lease_ids)
        except OSError as exc:
            logger.debug("orphaned-lease sweep failed for %s: %s", home, exc)
    return dropped


def active_session_registry_snapshot(
    registry_home: str | Path | None = None, *, strict: bool = False,
) -> list[dict[str, Any]]:
    """Return live leases; attachment callers require provable liveness.

    The per-entry liveness probes in ``_prune_dead`` run AFTER the file lock
    is released: holding an exclusive, unfair lock across process-introspection
    syscalls starves concurrent pollers once a handful of leases exist
    (#115578). The prune write-back re-locks and drops only the lease ids
    already proven dead, so a lease created between the snapshot and the
    write-back is never lost.
    """
    state_path, lock_path = _lease_paths(registry_home=registry_home)
    with _FileLock(lock_path):
        raw_entries = _read_entries(state_path, strict=True)
    entries = _prune_dead(raw_entries, strict=strict)
    if entries != raw_entries:
        live_lease_ids = {str(entry.get("lease_id") or "") for entry in entries}
        dead_lease_ids = {
            str(entry.get("lease_id") or "")
            for entry in raw_entries
            if str(entry.get("lease_id") or "") not in live_lease_ids
        }
        with _FileLock(lock_path):
            current_entries = _read_entries(state_path, strict=True)
            kept_entries = [
                entry for entry in current_entries
                if str(entry.get("lease_id") or "") not in dead_lease_ids
            ]
            if len(kept_entries) != len(current_entries):
                _write_entries(state_path, kept_entries)
    return entries


@contextmanager
def active_session_liveness_guard(
    session_id: str, *, registry_home: str | Path | None = None,
    own_live_lease_ids: set[str] | None = None,
) -> Iterator[bool]:
    """Hold the registry lock while reporting whether ``session_id`` is leased, so no
    new backend can acquire a lease between the check and the caller's ``end_session``."""
    state_path, lock_path = _lease_paths(registry_home=registry_home)
    with _FileLock(lock_path):
        entries = _prune_dead(
            _read_entries(state_path, strict=True), strict=True, target_session_id=session_id,
        )
        entries = _drop_self_orphans(entries, own_live_lease_ids)
        _write_entries(state_path, entries)
        yield _holds_session(entries, session_id)


@contextmanager
def release_active_session_liveness_guard(
    lease: ActiveSessionLease, session_id: str, *, own_live_lease_ids: set[str] | None = None,
) -> Iterator[bool]:
    """Remove ``lease`` and hold its registry lock through a lifecycle write, making
    cleanup one atomic decision (release, check siblings, end the durable row)."""
    if not lease.enabled or lease.released:
        home = lease.state_path.parent.parent if lease.state_path is not None else None
        with active_session_liveness_guard(
            session_id, registry_home=home, own_live_lease_ids=own_live_lease_ids,
        ) as active:
            yield active
        return

    state_path, lock_path = _lease_paths(lease)
    with _FileLock(lock_path):
        _unmark_release_queued(state_path, lease.lease_id)
        entries = _prune_dead(
            _read_entries(state_path, strict=True), strict=True, target_session_id=session_id,
        )
        kept = [e for e in entries if str(e.get("lease_id") or "") != lease.lease_id]
        kept = _drop_self_orphans(kept, own_live_lease_ids)
        if len(kept) != len(entries):
            _write_entries(state_path, kept)
        lease.released = True
        yield _holds_session(kept, session_id)
