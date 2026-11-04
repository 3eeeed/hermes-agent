"""``hermes sessions retitle-smart``: regenerate automatic titles from ALL of a session's user messages.

Sessions were titled from their opening message only, so a session that went on to cover several
tasks is hard to recognise in a list. This walks non-archived root conversations (plus their
compression continuations, never subagent children), builds the same digest the live rolling refresh
uses (``agent.title_generator.generate_session_title``) and proposes a title naming the whole body of
work. Dry run by default; ``--apply`` writes at ``llm`` authority, which can never replace a title the
user typed (``set_auto_title`` enforces ``user`` > ``llm`` in the same transaction as the write).
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional

SMART_RETITLE_SOURCES = ("desktop", "discord", "cli", "tui")
MIN_USER_TURNS = 2


def _is_subagent_row(row: Optional[dict]) -> bool:
    if not row:
        return True
    return (row.get("source") or "") == "subagent" or str(row.get("title") or "").startswith("Subagent:")


def _candidate_roots(db, *, since_days: Optional[float], session_ids: list[str]) -> list[dict]:
    where = ["s.parent_session_id IS NULL", "COALESCE(s.archived, 0) = 0", "COALESCE(s.hidden, 0) = 0",
             f"s.source IN ({', '.join('?' for _ in SMART_RETITLE_SOURCES)})"]
    params: list[Any] = list(SMART_RETITLE_SOURCES)
    if since_days:
        where.append("COALESCE(s.last_activity_at, s.started_at) >= ?")
        params.append(time.time() - float(since_days) * 86400)
    if session_ids:
        where.append(f"s.id IN ({', '.join('?' for _ in session_ids)})")
        params.extend(session_ids)
    rows = db._read_all(
        "SELECT s.id, s.source, s.title, s.title_source FROM sessions s "
        f"WHERE {' AND '.join(where)} ORDER BY COALESCE(s.last_activity_at, s.started_at) DESC",
        tuple(params))
    return [dict(r) for r in rows]


def _lineage(db, root_id: str) -> list[dict]:
    """Root plus its compression continuations (children of a ``compression``-ended parent that are not
    branch/delegate/reset forks, tool or subagent runs), oldest first. Walked directly instead of via
    ``get_compression_chain`` so an unmarked subagent child can never stand in for the continuation."""
    from hermes_state_common import _non_continuation_child_sql
    root = db.get_session(root_id)
    if not root:
        return []
    rows, frontier, seen = [root], [root], {root_id}
    while frontier:
        parent = frontier.pop(0)
        if parent.get("end_reason") != "compression":
            continue
        children = db._read_all(
            "SELECT id FROM sessions WHERE parent_session_id = ?\n"
            + _non_continuation_child_sql("", "?") + " ORDER BY started_at, id",
            (parent["id"], parent["id"], parent["id"], parent["id"]))
        for child_row in children:
            child = db.get_session(child_row["id"])
            if not child or child["id"] in seen or _is_subagent_row(child):
                continue
            seen.add(child["id"])
            rows.append(child)
            frontier.append(child)
    rows.sort(key=lambda r: (r.get("started_at") or 0, r["id"] != root_id))
    return rows


def lineage_user_texts(db, rows: list[dict]) -> list[str]:
    """Every real user message across the lineage, oldest first (deduped: a continuation can replay
    the predecessor's tail)."""
    from agent.title_generator import _is_real_user_turn, _message_text, _summarize_user_message
    texts, seen = [], set()
    for row in rows:
        try:
            messages = db.get_messages_as_conversation(row["id"], include_compacted=True)
        except TypeError:
            messages = db.get_messages_as_conversation(row["id"])
        for message in messages or []:
            if not _is_real_user_turn(message):
                continue
            text = _message_text(message)
            key = " ".join(_summarize_user_message(text).split()).casefold()
            if key and key not in seen:
                seen.add(key)
                texts.append(text)
    return texts


def plan_smart_retitles(db, *, limit: Optional[int] = None, since_days: Optional[float] = None,
                        session_ids: Optional[list[str]] = None,
                        generate: Optional[Callable[..., Optional[str]]] = None,
                        progress: Optional[Callable[[dict], None]] = None) -> list[dict]:
    """Proposed titles, one dict per eligible session (``status`` = proposed / unchanged / failed).
    Pure read + model calls: never writes."""
    from agent.title_generator import _normalized_title, generate_session_title
    generate = generate or generate_session_title
    resolved = []
    for raw in session_ids or []:
        sid = db.resolve_session_id(raw) or raw
        try:
            sid = db.get_conversation_root(sid) or sid
        except Exception:
            pass
        resolved.append(sid)
    plans: list[dict] = []
    for root in _candidate_roots(db, since_days=since_days, session_ids=resolved):
        if limit and len(plans) >= limit:
            break
        if _is_subagent_row(root):
            continue
        rows = _lineage(db, root["id"])
        holder = next((r for r in reversed(rows) if r.get("title") is not None), rows[-1] if rows else root)
        old_title, title_source = holder.get("title"), holder.get("title_source")
        # A NULL source on a titled row is a pre-provenance manual title: treat it as the user's.
        if old_title is not None and title_source not in ("llm", "derived"):
            continue
        if str(old_title or "").startswith("Subagent:"):
            continue
        texts = lineage_user_texts(db, rows)
        if len(texts) < MIN_USER_TURNS:
            continue
        proposed = generate(texts, current_title=old_title)
        if not proposed:
            status = "failed"
        elif _normalized_title(proposed) == _normalized_title(old_title):
            status = "unchanged"
        else:
            status = "proposed"
        plan = {"id": root["id"], "title_session_id": holder["id"], "source": root.get("source"),
                "user_turns": len(texts), "old_title": old_title, "old_title_source": title_source,
                "proposed_title": proposed, "status": status}
        plans.append(plan)
        if progress is not None:
            progress(plan)
    return plans


def apply_smart_retitles(db, plans: list[dict]) -> None:
    """Write ``proposed`` plans at ``llm`` authority; updates each plan's ``status`` in place."""
    from agent.title_generator import _persist_session_title
    for plan in plans:
        if plan["status"] != "proposed":
            continue
        try:
            source_fn = getattr(db, "get_session_title_source", None)
            current_source = source_fn(plan["title_session_id"]) if source_fn else None
            current_title = db.get_session_title(plan["title_session_id"])
            if current_title is not None and current_source not in ("llm", "derived"):
                plan["status"] = "skipped: user title"
                continue
            stored = _persist_session_title(
                db, plan["title_session_id"], plan["proposed_title"], source="llm", replace_equal=True)
        except Exception as exc:
            plan["status"] = f"error: {exc}"
            continue
        if stored:
            plan["status"], plan["applied_title"] = "applied", stored
        else:
            plan["status"] = "skipped: higher-authority title"


def _print_plan(plan: dict) -> None:
    print(f"  {plan['id']}  [{plan['source']}, {plan['user_turns']} turns, {plan['status']}]\n"
          f"    {plan['old_title']!r}\n    → {plan['proposed_title']!r}")


def cmd_retitle_smart(db, args) -> Optional[int]:
    apply_changes = bool(getattr(args, "apply", False))
    if apply_changes and getattr(args, "dry_run", False):
        print("Error: --dry-run and --apply are mutually exclusive.")
        return 2
    limit = getattr(args, "limit", None)
    since = getattr(args, "since", None)
    session_ids = list(getattr(args, "session_id", None) or [])
    mode = "" if apply_changes else " (dry run — pass --apply to write)"
    print(f"Smart re-title{mode}: scanning sessions…")
    plans = plan_smart_retitles(db, limit=limit, since_days=since, session_ids=session_ids,
                                progress=None if apply_changes else _print_plan)
    if apply_changes:
        apply_smart_retitles(db, plans)
        for plan in plans:
            _print_plan(plan)
    counts: dict[str, int] = {}
    for plan in plans:
        counts[plan["status"]] = counts.get(plan["status"], 0) + 1
    summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "nothing eligible"
    print(f"{len(plans)} session(s) examined: {summary}.")
    json_out = getattr(args, "json_out", None)
    if json_out:
        with open(json_out, "w", encoding="utf-8") as handle:
            json.dump(plans, handle, indent=2, ensure_ascii=False)
        print(f"Wrote {json_out}")
    return None
