"""Unfinished-work detection: sessions whose written files are still uncommitted in git."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import session_work_status as work
from hermes_state import SessionDB
from tests.waiting import wait_until


def _call(name: str, **args) -> dict:
    return {"id": f"c-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@e", "PATH": "/usr/bin:/bin:/usr/local/bin"})


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.py").write_text("a\n")
    (root / "b.py").write_text("b\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root.resolve()


@pytest.fixture
def home(tmp_path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def db(home):
    database = SessionDB(home / "state.db")
    try:
        yield database
    finally:
        database.close()


def _wrote(db, session_id: str, *calls: dict) -> None:
    db.append_message(session_id, "assistant", "", tool_calls=list(calls))


# ── path extraction ───────────────────────────────────────────────────────────────────────────


def test_paths_from_write_file_and_patch_path():
    calls = [_call("write_file", path="/r/new.py", content="x"), _call("patch", path="/r/old.py", old_string="a"),
             _call("read_file", path="/r/read.py"), _call("terminal", command="echo > /r/t.py")]
    assert work.paths_from_tool_calls(json.dumps(calls)) == {"/r/new.py", "/r/old.py"}


def test_paths_from_v4a_patch_text_with_several_files():
    patch = ("*** Begin Patch\n*** Update File: /r/one.py\n@@\n-a\n+b\n*** Add File: /r/two.py\n+x\n"
             "*** Delete File: /r/three.py\n*** End Patch\n")
    assert work.paths_from_tool_calls([_call("patch", mode="patch", patch=patch)]) == {
        "/r/one.py", "/r/two.py", "/r/three.py"}


def test_relative_paths_resolve_against_cwd_or_are_dropped():
    calls = [_call("write_file", path="src/x.py", content="")]
    assert work.paths_from_tool_calls(calls, "/proj") == {"/proj/src/x.py"}
    assert work.paths_from_tool_calls(calls, None) == set()
    assert work.paths_from_tool_calls("not json") == set()


# ── lineage ───────────────────────────────────────────────────────────────────────────────────


def test_delegates_and_continuations_roll_up_but_forks_do_not():
    roots = work.lineage_roots([
        ("root", None, None),
        ("tip", "root", None),  # compression continuation
        ("tip2", "tip", json.dumps({"_branched_from": "root"})),  # inherited marker naming another row
        ("sub", "tip", {"_delegate_from": "tip"}),
        ("branch", "root", {"_branched_from": "root"}),
        ("reset", "root", json.dumps({"_reset_from": "root"})),
        ("orphan", "gone", None),
    ])
    assert roots == {"root": "root", "tip": "root", "tip2": "root", "sub": "root",
                     "branch": "branch", "reset": "reset", "orphan": "gone"}


def test_parse_porcelain_v2_kinds():
    out = ("1 .M N... 100644 100644 100644 abc abc src/a b.py\0"
           "2 R. N... 100644 100644 100644 abc abc R100 new.py\0old.py\0"
           "? untracked.txt\0! ignored.log\0")
    assert work.parse_porcelain_v2(out) == ["src/a b.py", "new.py", "untracked.txt"]


# ── scan ──────────────────────────────────────────────────────────────────────────────────────


def test_dirty_files_flag_every_id_of_the_lineage(home, db, repo):
    db.create_session("root", "desktop")
    db.create_session("tip", "desktop", parent_session_id="root")
    db.create_session("other", "desktop")
    _wrote(db, "root", _call("patch", path=str(repo / "a.py")))
    _wrote(db, "tip", _call("write_file", path=str(repo / "new.py"), content="n"))
    _wrote(db, "other", _call("patch", path=str(repo / "b.py")))  # untouched on disk -> clean
    (repo / "a.py").write_text("changed\n")
    (repo / "new.py").write_text("n\n")

    snapshot = work.compute_status(home, now=123.0)

    assert set(snapshot["sessions"]) == {"root", "tip"}
    status = snapshot["sessions"]["tip"]
    assert status["dirty_count"] == 2
    assert status["repos"] == [str(repo)]
    assert status["sample"] == sorted([str(repo / "a.py"), str(repo / "new.py")])
    assert status["checked_at"] == 123.0
    assert work.cached_snapshot(home)["sessions"]["root"]["dirty_count"] == 2


def test_committing_clears_the_marker(home, db, repo):
    db.create_session("s", "cli")
    _wrote(db, "s", _call("patch", path=str(repo / "a.py")))
    (repo / "a.py").write_text("changed\n")
    assert "s" in work.compute_status(home)["sessions"]
    _git(repo, "commit", "-qam", "done")
    assert work.compute_status(home)["sessions"] == {}


def test_paths_outside_any_repo_or_host_are_skipped(home, db, tmp_path):
    db.create_session("s", "cli")
    (tmp_path / "loose.txt").write_text("x")
    _wrote(db, "s", _call("write_file", path=str(tmp_path / "loose.txt"), content="x"),
           _call("write_file", path="/Users/someone/elsewhere/x.py", content="x"))
    assert work.compute_status(home)["sessions"] == {}


def test_index_is_incremental_and_read_only_mode_writes_nothing(home, db, repo, monkeypatch):
    db.create_session("s", "cli")
    _wrote(db, "s", _call("patch", path=str(repo / "a.py")))
    work.compute_status(home)
    cache = json.loads(work.cache_path_for(home).read_text())
    first_mark = cache["index"]["max_message_id"]
    assert cache["index"]["scanned"]["s"] == first_mark

    seen: list[str] = []
    real = work.paths_from_tool_calls
    monkeypatch.setattr(work, "paths_from_tool_calls", lambda tc, cwd=None: seen.append(tc) or real(tc, cwd))
    _wrote(db, "s", _call("patch", path=str(repo / "b.py")))
    (repo / "b.py").write_text("changed\n")
    status = work.compute_status(home)["sessions"]["s"]

    assert len(seen) == 1 and "b.py" in seen[0]  # only the new message was parsed
    assert status["sample"] == [str(repo / "b.py")]  # a.py is clean; still indexed
    index = json.loads(work.cache_path_for(home).read_text())["index"]
    assert index["paths"]["s"] == sorted([str(repo / "a.py"), str(repo / "b.py")])

    other = home / "elsewhere.json"
    work.compute_status(home, cache_path=other, write_cache=False)
    assert not other.exists()


def test_index_rebuilds_when_message_ids_go_backwards(home, db, repo):
    db.create_session("s", "cli")
    _wrote(db, "s", _call("patch", path=str(repo / "a.py")))
    (repo / "a.py").write_text("changed\n")
    path = work.cache_path_for(home)
    work.compute_status(home)
    cache = json.loads(path.read_text())
    cache["index"]["max_message_id"] = 10_000  # a store restored from an older copy
    cache["index"]["paths"] = {}
    path.write_text(json.dumps(cache))
    assert "s" in work.compute_status(home)["sessions"]


# ── listing surface ───────────────────────────────────────────────────────────────────────────


def test_annotate_rows_sets_and_clears_the_wire_field(monkeypatch, home):
    statuses = {"a": {"dirty_count": 3, "repos": ["/r"], "sample": ["/r/x"], "checked_at": 1, "root_id": "a"}}
    monkeypatch.setattr(work, "unfinished_for", lambda _home: statuses)
    rows = [{"id": "a"}, {"id": "b", "unfinished": {"dirty_count": 1, "repos": []}}]
    work.annotate_rows(rows, home)
    assert rows == [{"id": "a", "unfinished": {"dirty_count": 3, "repos": ["/r"]}}, {"id": "b"}]


def test_refresher_scans_in_background_and_after_activity(home, db, repo):
    refresher = work._Refresher(interval=3600, debounce=0.05)
    assert refresher.snapshot(home) == {}  # never blocks on the first read
    db.create_session("s", "cli")
    wait_until(lambda: "checked_at" in refresher.snapshot(home), what="first background scan")
    assert refresher.snapshot(home)["sessions"] == {}

    _wrote(db, "s", _call("patch", path=str(repo / "a.py")))
    (repo / "a.py").write_text("changed\n")
    # A fresh persisted snapshot is adopted instead of rescanned; age it so the notify rescans.
    cache = json.loads(work.cache_path_for(home).read_text())
    cache["snapshot"]["checked_at"] = 0
    work.cache_path_for(home).write_text(json.dumps(cache))
    refresher.notify(home)
    wait_until(lambda: "s" in refresher.snapshot(home).get("sessions", {}), what="post-turn rescan")


def test_cli_lists_one_entry_per_conversation(home, db, repo, monkeypatch, capsys):
    from hermes_cli import sessions_unfinished as cli

    db.create_session("root", "desktop")
    db.set_session_title("root", "SEO")
    db.create_session("tip", "desktop", parent_session_id="root")
    _wrote(db, "tip", _call("patch", path=str(repo / "a.py")))
    (repo / "a.py").write_text("changed\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    class Args:
        json, refresh = True, True

    assert cli.cmd_unfinished(Args()) == 0
    out = json.loads(capsys.readouterr().out)
    assert [(e["id"], e["title"], e["dirty_count"], e["ids"]) for e in out["sessions"]] == [
        ("root", "SEO", 1, ["root", "tip"])]
