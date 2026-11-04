"""Which sessions left uncommitted work behind ("unfinished" sessions).

A session is *unfinished* when at least one file it wrote (``write_file``) or patched (``patch``:
its ``path`` argument, or the ``*** Update|Add|Delete File:`` headers of a V4A patch) is dirty in
its git repo right now (``git status --porcelain``). Delegate runs and compression continuations
roll up into the conversation that spawned them; ``/branch`` and ``/new`` forks stay separate
conversations. The status is attached to every id of a lineage, so whichever id a listing shows
(root or projected continuation tip) carries the marker.

Two layers, both keyed by the profile home that owns the store (one ``serve`` lists many
profiles, so nothing here reads the process ``HERMES_HOME``):

- :func:`compute_status` — the scan. The touched-path index is incremental (messages are read
  past the last scanned id only) and persisted with the last snapshot in
  ``<home>/state/session_work_status.json``; git is asked once per repo per scan.
- :class:`_Refresher` — the long-running backend's background thread: rescans every
  :data:`REFRESH_INTERVAL_S` and shortly after a turn settles (:func:`notify_activity`,
  debounced). Listings read the last snapshot (:func:`unfinished_for`) and never wait on git.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

CACHE_VERSION = 1
CACHE_RELPATH = ("state", "session_work_status.json")
REFRESH_INTERVAL_S = 300.0
ACTIVITY_DEBOUNCE_S = 15.0
SAMPLE_LIMIT = 5
_GIT_ROOT_TIMEOUT_S = 5.0
_GIT_STATUS_TIMEOUT_S = 20.0

_TOOL_NAMES = frozenset({"write_file", "patch"})
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+?)\s*$", re.MULTILINE)
# Fork markers in a child's model_config: the child is its own conversation only when the marker
# names its parent (continuations inherit model_config verbatim; see hermes_state_common).
_FORK_MARKERS = ("_branched_from", "_reset_from")


# ── path extraction ───────────────────────────────────────────────────────────────────────────


def _call_name_and_args(call: Any) -> tuple[str, Any]:
    fn = call.get("function") if isinstance(call.get("function"), dict) else call
    return str(fn.get("name") or ""), fn.get("arguments")


def _parse_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def paths_from_tool_calls(tool_calls: Any, cwd: Optional[str] = None) -> set[str]:
    """Absolute file paths written by ``write_file`` / ``patch`` calls in one ``tool_calls`` blob.

    Relative paths resolve against the session ``cwd`` when known, else are dropped (an unknown
    base could name the wrong file)."""
    if isinstance(tool_calls, str):
        try:
            tool_calls = json.loads(tool_calls)
        except ValueError:
            return set()
    found: set[str] = set()
    for call in tool_calls if isinstance(tool_calls, list) else []:
        if not isinstance(call, dict):
            continue
        name, raw_args = _call_name_and_args(call)
        if name not in _TOOL_NAMES:
            continue
        args = _parse_args(raw_args)
        candidates = [args["path"]] if isinstance(args.get("path"), str) else []
        if isinstance(args.get("patch"), str):
            candidates += _PATCH_FILE_RE.findall(args["patch"])
        for raw in candidates:
            if path := _absolute(raw, cwd):
                found.add(path)
    return found


def _absolute(raw: str, cwd: Optional[str]) -> Optional[str]:
    raw = raw.strip()
    if not raw:
        return None
    path = os.path.expanduser(raw)
    if not os.path.isabs(path):
        if not cwd or not os.path.isabs(cwd):
            return None
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


# ── lineage ───────────────────────────────────────────────────────────────────────────────────


def _is_fork(model_config: Any, parent_id: str) -> bool:
    if isinstance(model_config, str):
        try:
            model_config = json.loads(model_config)
        except ValueError:
            return False
    if not isinstance(model_config, dict):
        return False
    return any(model_config.get(marker) == parent_id for marker in _FORK_MARKERS)


def lineage_roots(rows: Iterable[tuple[str, Optional[str], Any]]) -> dict[str, str]:
    """``{session_id: conversation root}`` from ``(id, parent_session_id, model_config)`` rows.

    Delegate runs and compression continuations climb to their parent; a ``/branch`` or reset
    fork is its own root. Cycles and dangling parents stop the climb."""
    rows = list(rows)
    up = {sid: parent for sid, parent, cfg in rows if parent and not _is_fork(cfg, parent)}
    roots: dict[str, str] = {}
    for sid, _parent, _cfg in rows:
        current, seen = sid, {sid}
        while current in up and up[current] not in seen:
            current = up[current]
            seen.add(current)
        roots[sid] = current
    return roots


# ── git ───────────────────────────────────────────────────────────────────────────────────────


def _git(cwd: str, *args: str, timeout: float) -> Optional[str]:
    """Raw stdout of ``git -C cwd args`` or None. Optional locks off: a background status must
    never take ``index.lock`` from under the user's own git commands."""
    from hermes_cli._subprocess_compat import (NO_LAZY_FETCH_ENV, bounded_probe_run,
                                               noninteractive_git_env)

    env = {**noninteractive_git_env(), **NO_LAZY_FETCH_ENV, "GIT_OPTIONAL_LOCKS": "0"}
    result = bounded_probe_run(["git", "-C", cwd, *args], timeout=timeout, env=env)
    if result is None or result.returncode != 0:
        return None
    return result.stdout or ""


def _existing_dir(path: str) -> Optional[str]:
    current = os.path.dirname(path)
    while current and not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return current or None


class _GitView:
    """Per-scan git answers: one ``rev-parse`` per directory, one ``status`` per repo."""

    def __init__(self) -> None:
        self._roots: dict[str, Optional[str]] = {}
        self._dirty: dict[str, Optional[set[str]]] = {}

    def repo_of(self, path: str) -> Optional[str]:
        directory = _existing_dir(path)
        if directory is None:
            return None
        if directory not in self._roots:
            out = _git(directory, "rev-parse", "--show-toplevel", timeout=_GIT_ROOT_TIMEOUT_S)
            self._roots[directory] = os.path.normpath(out.strip()) if out and out.strip() else None
        return self._roots[directory]

    def dirty_files(self, root: str) -> Optional[set[str]]:
        """Absolute paths git reports as modified/staged/untracked/deleted; None when git failed."""
        if root not in self._dirty:
            out = _git(root, "status", "--porcelain=v2", "-z", "-uall", timeout=_GIT_STATUS_TIMEOUT_S)
            self._dirty[root] = None if out is None else {
                os.path.normpath(os.path.join(root, rel)) for rel in parse_porcelain_v2(out)}
        return self._dirty[root]

    @property
    def repos(self) -> list[str]:
        return sorted(self._dirty)


# Entry kinds of ``git status --porcelain=v2 -z``: fields before the path, and whether the entry
# is followed by an extra NUL-separated original path (renames/copies).
_V2_LAYOUT = {"1": (8, False), "2": (9, True), "u": (10, False), "?": (1, False)}


def parse_porcelain_v2(output: str) -> list[str]:
    """Repo-relative paths from ``git status --porcelain=v2 -z`` output (ignored ``!`` skipped)."""
    entries = output.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        layout = _V2_LAYOUT.get(entry[:1])
        if not entry or layout is None:
            continue
        fields, has_orig = layout
        parts = entry.split(" ", fields)
        if len(parts) > fields:
            paths.append(parts[fields])
        if has_orig:
            index += 1
    return paths


# ── incremental path index ────────────────────────────────────────────────────────────────────


def cache_path_for(home: str | Path) -> Path:
    return Path(home).joinpath(*CACHE_RELPATH)


def _load_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("version") == CACHE_VERSION else {}


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = None
    return conn


_TOOL_ROWS_SQL = (
    "SELECT id, session_id, tool_calls FROM messages WHERE id > ? AND tool_calls IS NOT NULL"
    " AND (tool_calls LIKE '%write_file%' OR tool_calls LIKE '%\"patch\"%') ORDER BY id")


def _update_index(conn: sqlite3.Connection, index: dict, cwds: dict[str, Optional[str]]) -> dict:
    """Fold messages newer than the index watermark into ``index`` (mutated and returned).

    ``scanned`` keeps each session's newest scanned message id; a store whose ids went backwards
    (restored or rebuilt) is re-read from scratch."""
    newest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
    if newest < int(index.get("max_message_id") or 0):
        index.clear()
    index.setdefault("paths", {})
    index.setdefault("scanned", {})
    watermark = int(index.get("max_message_id") or 0)
    for message_id, session_id, tool_calls in conn.execute(_TOOL_ROWS_SQL, (watermark,)):
        found = paths_from_tool_calls(tool_calls, cwds.get(session_id))
        if found:
            known = set(index["paths"].get(session_id) or [])
            index["paths"][session_id] = sorted(known | found)
        index["scanned"][session_id] = message_id
    index["max_message_id"] = max(watermark, newest)
    return index


def _session_rows(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute("SELECT id, parent_session_id, model_config, cwd FROM sessions").fetchall()


# ── status ────────────────────────────────────────────────────────────────────────────────────


def _group_paths(index_paths: dict[str, list[str]], roots: dict[str, str]) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for session_id, paths in index_paths.items():
        grouped.setdefault(roots.get(session_id, session_id), set()).update(paths)
    return grouped


def _dirty_in(paths: Iterable[str], git: _GitView) -> dict[str, list[str]]:
    """``{repo_root: [dirty paths]}`` for the paths that are dirty in their repo right now."""
    by_repo: dict[str, list[str]] = {}
    for path in paths:
        root = git.repo_of(path)
        dirty = git.dirty_files(root) if root else None
        if root and dirty and path in dirty:
            by_repo.setdefault(root, []).append(path)
    return by_repo


def _statuses(grouped: dict[str, set[str]], roots: dict[str, str], checked_at: float) -> dict[str, dict]:
    git = _GitView()
    members: dict[str, list[str]] = {}
    for session_id, root in roots.items():
        members.setdefault(root, []).append(session_id)
    out: dict[str, dict] = {}
    for root, paths in grouped.items():
        by_repo = _dirty_in(sorted(paths), git)
        if not by_repo:
            continue
        dirty = sorted(p for files in by_repo.values() for p in files)
        status = {"dirty_count": len(dirty), "repos": sorted(by_repo), "sample": dirty[:SAMPLE_LIMIT],
                  "checked_at": checked_at, "root_id": root}
        for session_id in members.get(root, [root]):
            out[session_id] = status
    return out


def _digest(statuses: dict[str, dict]) -> str:
    """Stable fingerprint of what listings show (not of ``checked_at``): a rescan that changed
    nothing must not wake every client."""
    import hashlib
    wire = sorted((sid, status["dirty_count"], tuple(status["repos"]))
                  for sid, status in statuses.items())
    return hashlib.blake2b(repr(wire).encode("utf-8", "backslashreplace"), digest_size=12).hexdigest()


def compute_status(home: str | Path, *, cache_path: Optional[Path] = None, write_cache: bool = True,
                   now: Optional[float] = None) -> dict:
    """Scan ``home``'s ``state.db`` and git; returns ``{"checked_at", "sessions": {id: status}}``.

    ``cache_path`` defaults to ``<home>/state/session_work_status.json``; ``write_cache=False``
    keeps the run read-only (the index is still loaded from an existing cache)."""
    home = Path(home)
    cache_path = cache_path or cache_path_for(home)
    db_path = home / "state.db"
    checked_at = time.time() if now is None else now
    if not db_path.exists():
        return {"checked_at": checked_at, "sessions": {}}
    cache = _load_cache(cache_path)
    index = cache.get("index")
    if not isinstance(index, dict):
        index = {}
    conn = _connect_ro(db_path)
    try:
        rows = _session_rows(conn)
        index = _update_index(conn, index, {sid: cwd for sid, _p, _c, cwd in rows})
    finally:
        conn.close()
    roots = lineage_roots((sid, parent, cfg) for sid, parent, cfg, _cwd in rows)
    statuses = _statuses(_group_paths(index["paths"], roots), roots, checked_at)
    snapshot = {"checked_at": checked_at, "digest": _digest(statuses), "sessions": statuses}
    if write_cache:
        from utils import atomic_json_write
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(cache_path, {"version": CACHE_VERSION, "index": index, "snapshot": snapshot})
    return snapshot


def cached_snapshot(home: str | Path, *, cache_path: Optional[Path] = None) -> Optional[dict]:
    """The last snapshot persisted for ``home`` (any process), or None."""
    snapshot = _load_cache(cache_path or cache_path_for(home)).get("snapshot")
    return snapshot if isinstance(snapshot, dict) and isinstance(snapshot.get("sessions"), dict) else None


def wire_status(status: Optional[dict]) -> Optional[dict]:
    """The compact listing field: ``{dirty_count, repos}``; None when clean."""
    if not status or not status.get("dirty_count"):
        return None
    return {"dirty_count": int(status["dirty_count"]), "repos": list(status.get("repos") or [])}


# ── background refresh ────────────────────────────────────────────────────────────────────────


class _Refresher:
    """One daemon thread per process keeping a snapshot per tracked profile home.

    Homes are tracked on first read, so the profiles a backend actually lists are the ones it
    scans. A fresh snapshot another process persisted (CLI, sibling backend) is adopted instead
    of rescanning. Reads never block on a scan."""

    def __init__(self, interval: float = REFRESH_INTERVAL_S, debounce: float = ACTIVITY_DEBOUNCE_S) -> None:
        self.interval, self.debounce = interval, debounce
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._due: dict[str, float] = {}
        self._snapshots: dict[str, dict] = {}
        self._thread: Optional[threading.Thread] = None

    def _register(self, home: str) -> None:
        if home not in self._due:
            self._due[home] = 0.0
            if (persisted := cached_snapshot(home)) is not None:
                self._snapshots[home] = persisted
            self._wake.set()

    def _track(self, home: str) -> None:
        self._register(home)
        if self._thread is None or not self._thread.is_alive():
            # health: allow HX012 -- every scan names its home explicitly; a process-lifetime thread must
            # not inherit the first caller's profile contextvars.
            self._thread = threading.Thread(target=self._run, name="hermes-session-work-status", daemon=True)
            self._thread.start()

    def snapshot(self, home: str | Path) -> dict:
        key = str(Path(home))
        with self._lock:
            self._track(key)
            return self._snapshots.get(key) or {}

    def notify(self, home: str | Path) -> None:
        """Pull the next scan of ``home`` forward. Never starts the thread: that is the first
        listing read's job, so a turn thread (or a test running threads inline) never hosts it."""
        key = str(Path(home))
        with self._lock:
            self._register(key)
            self._due[key] = min(self._due[key], time.monotonic() + self.debounce)
        self._wake.set()

    def _next_due(self) -> tuple[Optional[str], float]:
        with self._lock:
            if not self._due:
                return None, self.interval
            home = min(self._due, key=self._due.__getitem__)
            return home, self._due[home] - time.monotonic()

    def _refresh(self, home: str) -> None:
        persisted = cached_snapshot(home)
        previous = self._snapshots.get(home) or {}
        stamp = float((persisted or {}).get("checked_at") or 0)
        adopt = persisted is not None and time.time() - stamp < self.interval / 2 \
            and stamp >= float(previous.get("checked_at") or 0)
        try:
            snapshot = persisted if adopt else compute_status(home)
        except Exception:
            logger.warning("session work status scan failed for %s", home, exc_info=True)
            snapshot = None
        with self._lock:
            if snapshot is not None:
                self._snapshots[home] = snapshot
            self._due[home] = time.monotonic() + self.interval

    def _run(self) -> None:
        while True:
            home, wait = self._next_due()
            if home is None or wait > 0:
                self._wake.wait(timeout=max(wait, 0.05))
                self._wake.clear()
                continue
            self._refresh(home)


_refresher = _Refresher()


def unfinished_for(home: str | Path) -> dict[str, dict]:
    """``{session_id: status}`` from the last snapshot for ``home`` (starts tracking it)."""
    return _refresher.snapshot(home).get("sessions") or {}


def snapshot_signature(home: str | Path) -> Optional[str]:
    """Changes only when what a listing shows for ``home`` changes — for change watchers. None
    while nothing is flagged, so a first scan that finds nothing is not a change."""
    snapshot = _refresher.snapshot(home)
    return snapshot.get("digest") if snapshot.get("sessions") else None


def notify_activity(home: str | Path) -> None:
    """A turn settled in ``home``: rescan soon (coalesced with other turns in the window)."""
    _refresher.notify(home)


def annotate_db_rows(rows: list[dict], db_path: Optional[str | Path]) -> list[dict]:
    """:func:`annotate_rows` for the profile owning ``db_path`` (no-op without one); returns ``rows``."""
    if db_path:
        annotate_rows(rows, Path(db_path).parent)
    return rows


def annotate_rows(rows: Iterable[dict], home: str | Path) -> None:
    """Set (or clear) ``unfinished`` on each listing row from the snapshot for ``home``."""
    statuses = unfinished_for(home)
    for row in rows:
        status = wire_status(statuses.get(str(row.get("id") or "")))
        if status is None:
            row.pop("unfinished", None)
        else:
            row["unfinished"] = status
