"""``hermes sessions retitle-smart``: dry run writes nothing, --apply never touches user titles."""
import json
from types import SimpleNamespace

import pytest

# Imported at collection: SessionDB's transcript reads lazily import the agent loop, whose bootstrap
# reads the pm payload manifest — outside the test home when run from a worktree.
import agent.conversation_loop

from hermes_cli.sessions_retitle_smart import cmd_retitle_smart, plan_smart_retitles
from hermes_state import SessionDB


def _turns(db, sid, *texts):
    for text in texts:
        db.append_message(sid, "user", text)
        db.append_message(sid, "assistant", "ok")


@pytest.fixture
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    store.create_session(session_id="auto", source="desktop")
    store.set_auto_title("auto", "Fix auth test", source="llm")
    _turns(store, "auto", "fix the flaky auth test", "now write release notes")
    store.create_session(session_id="mine", source="desktop")
    store.set_session_title("mine", "My own name")
    _turns(store, "mine", "first job", "second job")
    store.create_session(session_id="short", source="desktop")
    store.set_auto_title("short", "One shot", source="llm")
    _turns(store, "short", "only one ask")
    store.create_session(session_id="cron1", source="cron")
    store.set_auto_title("cron1", "Cron thing", source="llm")
    _turns(store, "cron1", "a cron one", "a cron two")
    yield store
    store.close()


def _fake_generate(calls):
    def generate(texts, current_title=None):
        calls.append((list(texts), current_title))
        return "Auth: test fix + release notes"
    return generate


@pytest.fixture
def stub_llm(monkeypatch):
    calls = []
    monkeypatch.setattr("agent.title_generator.generate_session_title", _fake_generate(calls))
    return calls


def test_dry_run_proposes_but_never_writes(db, stub_llm, tmp_path, capsys):
    out = tmp_path / "plan.json"
    args = SimpleNamespace(apply=False, dry_run=True, limit=None, since=None, session_id=None, json_out=str(out))
    assert cmd_retitle_smart(db, args) is None
    plans = json.loads(out.read_text())
    assert [p["id"] for p in plans] == ["auto"]  # user title, single-turn and cron sessions excluded
    assert plans[0]["old_title"] == "Fix auth test"
    assert plans[0]["proposed_title"] == "Auth: test fix + release notes"
    assert plans[0]["status"] == "proposed"
    assert db.get_session_title("auto") == "Fix auth test"
    assert db.get_session_title("mine") == "My own name"
    assert "dry run" in capsys.readouterr().out


def test_apply_writes_llm_and_skips_user_titles(db, stub_llm):
    args = SimpleNamespace(apply=True, dry_run=False, limit=None, since=None, session_id=None, json_out=None)
    cmd_retitle_smart(db, args)
    assert db.get_session_title("auto") == "Auth: test fix + release notes"
    assert db.get_session_title_source("auto") == "llm"
    assert db.get_session_title("mine") == "My own name"
    assert db.get_session_title_source("mine") == "user"
    assert all(current != "My own name" for _, current in stub_llm)


def test_apply_rechecks_a_title_renamed_by_the_user_after_planning(db):
    plans = plan_smart_retitles(db, generate=lambda texts, current_title=None: "New smart title")
    db.set_session_title("auto", "Renamed meanwhile")
    from hermes_cli.sessions_retitle_smart import apply_smart_retitles
    apply_smart_retitles(db, plans)
    assert db.get_session_title("auto") == "Renamed meanwhile"
    assert plans[0]["status"].startswith("skipped")


def test_lineage_collects_continuations_but_not_subagents(db):
    db.create_session(session_id="root", source="cli")
    db.set_auto_title("root", "Root work", source="llm")
    _turns(db, "root", "root ask one")
    db.end_session("root", "compression")
    db.create_session(session_id="cont", source="cli", parent_session_id="root")
    _turns(db, "cont", "continuation ask two")
    db.create_session(session_id="sub", source="subagent", parent_session_id="root")
    _turns(db, "sub", "subagent goal text", "subagent more")
    seen = []

    def generate(texts, current_title=None):
        seen.append(list(texts))
        return "Root: ask one + ask two"
    plans = plan_smart_retitles(db, session_ids=["root"], generate=generate)
    assert [p["id"] for p in plans] == ["root"]
    assert seen == [["root ask one", "continuation ask two"]]


def test_limit_and_session_filter(db):
    plans = plan_smart_retitles(db, session_ids=["mine"], generate=lambda t, current_title=None: "X")
    assert plans == []
    plans = plan_smart_retitles(db, limit=1, generate=lambda t, current_title=None: "X: a + b")
    assert len(plans) == 1


def test_dry_run_and_apply_are_exclusive(db):
    args = SimpleNamespace(apply=True, dry_run=True, limit=None, since=None, session_id=None, json_out=None)
    assert cmd_retitle_smart(db, args) == 2


def test_parser_registers_retitle_smart():
    import argparse
    from hermes_cli.subcommands import sessions as sub
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")
    sub.build_sessions_parser(subs, cmd_sessions=lambda *a, **k: None)
    args = parser.parse_args(["sessions", "retitle-smart", "--session-id", "a", "--session-id", "b",
                              "--limit", "3", "--since", "7", "--json-out", "x.json"])
    assert args.session_id == ["a", "b"] and args.limit == 3 and args.since == 7 and not args.apply
