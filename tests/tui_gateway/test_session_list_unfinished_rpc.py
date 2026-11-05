"""``session.list`` rows carry ``unfinished`` from the last background work-status scan."""

import pytest

import tui_gateway.methods_session  # noqa: F401  (registers the session methods)
import tui_gateway.server as srv
from hermes_cli import session_work_status as work
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path, monkeypatch):
    database = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(srv, "_get_db", lambda: database)
    try:
        yield database
    finally:
        database.close()


def test_session_list_rows_carry_unfinished_only_when_dirty(db, tmp_path, monkeypatch):
    for sid in ("dirty", "clean"):
        db.create_session(sid, "desktop")
        db.append_message(sid, "user", "hi")
    seen_homes = []

    def fake_unfinished_for(home):
        seen_homes.append(str(home))
        return {"dirty": {"dirty_count": 2, "repos": ["/r"], "sample": [], "checked_at": 1, "root_id": "dirty"}}

    monkeypatch.setattr(work, "unfinished_for", fake_unfinished_for)
    rows = srv._methods["session.list"](1, {})["result"]["sessions"]
    by_id = {row["id"]: row for row in rows}

    assert by_id["dirty"]["unfinished"] == {"dirty_count": 2, "repos": ["/r"]}
    assert "unfinished" not in by_id["clean"]
    assert seen_homes == [str(tmp_path)]  # the store's own profile home, not the process default
