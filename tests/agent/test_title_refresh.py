"""Rolling title refresh + injected-note stripping (agent.title_generator)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.title_generator import (
    DIGEST_BUDGET_CHARS,
    build_session_digest,
    derive_title,
    generate_session_title,
    maybe_auto_title,
    session_user_texts,
    strip_injected_turn_notes,
    title_refresh_turns,
    wait_for_title_upgrades,
)
from hermes_state import SessionDB

DELEGATE_NOTE = (
    "[Delegate mode is on for this message. You lead; workers implement.]\n"
    "Understand the request, read the workspace, and plan.\n"
    "Lanes:\n- `default`: openrouter/x, effort default"
)


def _llm_returning(*titles, captured=None):
    answers = iter(titles)

    def stub(**kwargs):
        if captured is not None:
            captured.append(kwargs)
        resp = MagicMock()
        resp.choices[0].message.content = next(answers)
        resp.choices[0].message.reasoning = None
        resp.choices[0].message.reasoning_content = None
        return resp
    return stub


def _history(*texts):
    out = []
    for text in texts:
        out += [{"role": "user", "content": text}, {"role": "assistant", "content": "ok"}]
    return out


@pytest.fixture
def enabled():
    with patch("agent.title_generator._auto_title_enabled", return_value=True), \
            patch("agent.title_generator._model_title_upgrade_enabled", return_value=True), \
            patch("agent.title_generator._title_config", return_value={}):
        yield


@pytest.fixture
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    store.create_session(session_id="s1", source="desktop")
    yield store
    store.close()


class TestInjectedNotes:
    def test_delegate_note_is_stripped_from_title_input(self):
        text = f"{DELEGATE_NOTE}\n\nFix the flaky auth test in CI"
        assert strip_injected_turn_notes(text) == "Fix the flaky auth test in CI"
        assert derive_title(text) == "Fix the flaky auth test in CI"

    def test_note_only_message_is_not_titleable(self):
        from agent.title_generator import is_titleable_user_message
        assert not is_titleable_user_message(DELEGATE_NOTE)

    def test_plain_message_untouched(self):
        assert strip_injected_turn_notes("hello\n\nworld") == "hello\n\nworld"

    def test_turn_start_titles_from_typed_text_not_delegate_note(self, db, enabled):
        """Desktop path without title_user_message still never titles from the note."""
        from agent.turn_context import _maybe_title_session_at_turn_start
        agent = SimpleNamespace(_session_db=db, session_id="s1", platform="desktop", _session_db_created=True,
                                model="m", provider="p", base_url="", api_key="", api_mode="")
        messages = [{"role": "user", "content": f"{DELEGATE_NOTE}\n\nRefactor the billing exporter"}]
        with patch("agent.title_generator.call_llm", side_effect=_llm_returning("Refactor billing exporter")):
            _maybe_title_session_at_turn_start(agent, messages)
            wait_for_title_upgrades(10)
        title = db.get_session_title("s1")
        assert title == "Refactor billing exporter"
        assert "Delegate" not in title


class TestDigest:
    def test_oldest_first_and_capped_per_message(self):
        digest = build_session_digest(["first ask", "x" * 500, "latest ask"])
        lines = digest.splitlines()
        assert lines[0] == "1. first ask" and lines[-1] == "3. latest ask"
        assert len(lines[1]) <= len("2. ") + 200

    def test_budget_keeps_first_and_most_recent(self):
        msgs = [f"task number {i} " + "y" * 180 for i in range(30)]
        digest = build_session_digest(msgs)
        assert len(digest) <= DIGEST_BUDGET_CHARS
        assert digest.startswith("1. task number 0 ")
        assert "30. task number 29 " in digest
        assert "omitted" in digest
        assert "2. task number 1 " not in digest  # middle dropped oldest-first

    def test_digest_strips_notes_and_wrappers(self):
        digest = build_session_digest([f"{DELEGATE_NOTE}\n\nShip the release", "<system-reminder>x</system-reminder>Add docs"])
        assert "Delegate" not in digest and "system-reminder" not in digest
        assert "Ship the release" in digest and "Add docs" in digest

    def test_user_texts_append_current_message_once(self):
        hist = _history("a one", "b two")
        assert session_user_texts(hist, "c three") == ["a one", "b two", "c three"]
        assert session_user_texts(hist + [{"role": "user", "content": "c three"}], "c three") == [
            "a one", "b two", "c three"]


class TestRefreshSchedule:
    def test_default_and_configured_turns(self):
        with patch("agent.title_generator._title_config", return_value={}):
            assert title_refresh_turns() == frozenset({3, 6, 10, 16, 25})
        with patch("agent.title_generator._title_config", return_value={"refresh_turns": []}):
            assert title_refresh_turns() == frozenset()
        with patch("agent.title_generator._title_config", return_value={"refresh_turns": [4, "8", True, 1]}):
            assert title_refresh_turns() == frozenset({4, 8})

    def test_refresh_fires_at_turn_three_and_replaces_llm_title(self, db, enabled):
        db.set_auto_title("s1", "Fix auth test", source="llm")
        captured, renamed = [], []
        hist = _history("fix the flaky auth test", "now bump the CI python version") + [
            {"role": "user", "content": "and write release notes"}]
        with patch("agent.title_generator.call_llm",
                   side_effect=_llm_returning("CI: auth test fix + release notes", captured=captured)):
            thread = maybe_auto_title(db, "s1", "and write release notes", hist,
                                      title_callback=lambda t, s: renamed.append((t, s)))
            assert thread is not None
            wait_for_title_upgrades(10)
        assert db.get_session_title("s1") == "CI: auth test fix + release notes"
        assert db.get_session_title_source("s1") == "llm"
        assert renamed == [("CI: auth test fix + release notes", "llm")]
        user_payload = captured[0]["messages"][1]["content"]
        assert "Current title: Fix auth test" in user_payload
        for text in ("fix the flaky auth test", "bump the CI python", "release notes"):
            assert text in user_payload
        assert captured[0]["extra_body"]["response_format"]["json_schema"]["name"] == "session_title"

    def test_refresh_never_touches_a_user_title(self, db, enabled):
        db.set_session_title("s1", "My name")
        hist = _history("one thing", "two thing") + [{"role": "user", "content": "three thing"}]
        with patch("agent.title_generator.call_llm") as llm:
            assert maybe_auto_title(db, "s1", "three thing", hist) is None
            wait_for_title_upgrades(10)
        llm.assert_not_called()
        assert db.get_session_title("s1") == "My name"

    def test_refresh_does_not_fire_off_schedule(self, db, enabled):
        db.set_auto_title("s1", "Fix auth test", source="llm")
        hist = _history("a1", "a2", "a3") + [{"role": "user", "content": "a4 question"}]
        with patch("agent.title_generator.call_llm") as llm:
            assert maybe_auto_title(db, "s1", "a4 question", hist) is None
            wait_for_title_upgrades(10)
        llm.assert_not_called()
        assert db.get_session_title("s1") == "Fix auth test"

    def test_refresh_disabled_by_empty_schedule(self, db):
        db.set_auto_title("s1", "Fix auth test", source="llm")
        hist = _history("a1", "a2") + [{"role": "user", "content": "a3 question"}]
        with patch("agent.title_generator._title_config", return_value={"refresh_turns": []}), \
                patch("agent.title_generator.call_llm") as llm:
            assert maybe_auto_title(db, "s1", "a3 question", hist) is None
        llm.assert_not_called()

    def test_same_title_is_a_noop(self, db, enabled):
        db.set_auto_title("s1", "Fix auth test", source="llm")
        renamed = []
        hist = _history("a1", "a2") + [{"role": "user", "content": "a3 question"}]
        with patch("agent.title_generator.call_llm", side_effect=_llm_returning("fix  AUTH test")):
            maybe_auto_title(db, "s1", "a3 question", hist, title_callback=lambda t, s: renamed.append(t))
            wait_for_title_upgrades(10)
        assert db.get_session_title("s1") == "Fix auth test" and renamed == []

    def test_refresh_dedupes_collisions(self, db, enabled):
        db.create_session(session_id="other", source="desktop")
        db.set_session_title("other", "Billing: exporter + docs")
        db.set_auto_title("s1", "Billing exporter", source="llm")
        hist = _history("a1", "a2") + [{"role": "user", "content": "a3 question"}]
        with patch("agent.title_generator.call_llm", side_effect=_llm_returning("Billing: exporter + docs")):
            maybe_auto_title(db, "s1", "a3 question", hist)
            wait_for_title_upgrades(10)
        assert db.get_session_title("s1") == "Billing: exporter + docs #2"


class TestGenerateSessionTitle:
    @pytest.mark.parametrize("reply", [
        '{"title": "' + " ".join(["word"] * 25) + '"}',
        '{"title": "Friendly greeting"}',
        '{"title": "Code changes"}',
        '{"title": "Session titles: delegate fix + rolling refresh"}',
    ])
    def test_guards_reject(self, reply, enabled):
        with patch("agent.title_generator.call_llm", side_effect=_llm_returning(reply)):
            assert generate_session_title(["a1 thing", "a2 thing"], "Old") is None

    def test_language_pin_honoured(self, enabled):
        captured = []
        with patch("agent.title_generator._title_language", return_value="German"), \
                patch("agent.title_generator.call_llm", side_effect=_llm_returning('{"title": "X: a + b"}', captured=captured)):
            assert generate_session_title(["a1", "a2"]) == "X: a + b"
        assert "Write the title in German." in captured[0]["messages"][0]["content"]


class TestStoreEqualRank:
    def test_llm_replaces_llm_only_with_replace_equal(self, db):
        db.set_auto_title("s1", "First", source="llm")
        assert db.set_auto_title("s1", "Second", source="llm") is False
        assert db.set_auto_title("s1", "Second", source="llm", replace_equal=True) is True
        db.set_session_title("s1", "Mine")
        assert db.set_auto_title("s1", "Third", source="llm", replace_equal=True) is False
        assert db.get_session_title("s1") == "Mine"
