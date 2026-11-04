"""``hermes sessions unfinished``: sessions whose edited files are still uncommitted in git."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli import session_work_status as work


def _snapshot(home: Path, refresh: bool) -> dict:
    """The persisted snapshot when recent enough (the Desktop backend keeps it fresh), else a scan."""
    cached = None if refresh else work.cached_snapshot(home)
    if cached and time.time() - float(cached.get("checked_at") or 0) < 2 * work.REFRESH_INTERVAL_S:
        return cached
    return work.compute_status(home)


def _session_meta(home: Path, ids: set[str]) -> dict[str, dict[str, Any]]:
    db_path = home / "state.db"
    if not ids or not db_path.exists():
        return {}
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        marks = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, title, source, archived, COALESCE(last_activity_at, ended_at, started_at)"
            f" FROM sessions WHERE id IN ({marks})", sorted(ids)).fetchall()
    finally:
        conn.close()
    return {sid: {"title": title or "", "source": source or "", "archived": bool(archived),
                  "last_active": last or 0} for sid, title, source, archived, last in rows}


def unfinished_sessions(home: Path, snapshot: dict) -> list[dict[str, Any]]:
    """One entry per conversation (lineage root), most recently active first."""
    lineages: dict[str, dict[str, Any]] = {}
    for sid, status in (snapshot.get("sessions") or {}).items():
        root = status.get("root_id") or sid
        entry = lineages.setdefault(root, {"id": root, "ids": [], **{
            k: status.get(k) for k in ("dirty_count", "repos", "sample", "checked_at")}})
        entry["ids"].append(sid)
    meta = _session_meta(home, {sid for entry in lineages.values() for sid in entry["ids"]})
    for entry in lineages.values():
        members = [meta[sid] for sid in entry["ids"] if sid in meta]
        own = meta.get(entry["id"]) or {}
        titled = [m["title"] for m in sorted(members, key=lambda m: m["last_active"], reverse=True) if m["title"]]
        entry.update(title=own.get("title") or (titled[0] if titled else ""), source=own.get("source", ""),
                     archived=bool(own.get("archived")),
                     last_active=max((m["last_active"] for m in members), default=0))
        entry["ids"].sort()
    return sorted(lineages.values(), key=lambda e: e["last_active"], reverse=True)


def _when(stamp: Optional[float]) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp else "-"


def _print_table(entries: list[dict[str, Any]], checked_at: Optional[float]) -> None:
    if not entries:
        print(f"No unfinished sessions (checked {_when(checked_at)}).")
        return
    print(f"{len(entries)} session(s) with uncommitted work (checked {_when(checked_at)}):\n")
    for entry in entries:
        title = entry["title"] or "(untitled)"
        flag = "  [archived]" if entry["archived"] else ""
        print(f"  {title}{flag}\n    id {entry['id']}  ·  last active {_when(entry['last_active'])}"
              f"  ·  {entry['dirty_count']} dirty file(s)")
        for repo in entry["repos"]:
            print(f"    repo {repo}")


def cmd_unfinished(args) -> int:
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    snapshot = _snapshot(home, bool(getattr(args, "refresh", False)))
    entries = unfinished_sessions(home, snapshot)
    if getattr(args, "json", False):
        print(json.dumps({"checked_at": snapshot.get("checked_at"), "sessions": entries},
                         ensure_ascii=False, indent=2))
    else:
        _print_table(entries, snapshot.get("checked_at"))
    return 0


def add_unfinished_parser(sessions_subparsers) -> None:
    parser = sessions_subparsers.add_parser(
        "unfinished", help="List sessions whose edited files are still uncommitted in git",
        description="A session is unfinished when a file it wrote or patched is still modified, "
            "staged or untracked in its git repo. Delegate runs and compression continuations "
            "count toward the conversation that started them. Reads the snapshot the Desktop "
            "backend refreshes every few minutes; --refresh rescans now.")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--refresh", action="store_true", help="Rescan state.db and git now")
