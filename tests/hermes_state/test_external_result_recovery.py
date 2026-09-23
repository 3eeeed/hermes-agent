"""A post-commit replay failure must not erase an external answer on reopen."""

import pytest

from hermes_state import SessionDB


def test_external_answer_survives_replay_failure_and_restart_without_duplication(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    session_id = "external-recovery"
    send_id = "send-external-recovery"
    submit_holder = "submit-holder"
    first_lease = "external-lease-first"
    retry_lease = "external-lease-retry"

    db = SessionDB(db_path=path)
    try:
        db.create_session(session_id, source="desktop")
        assert db.reserve_prompt_send(session_id, send_id, "digest", holder=submit_holder).outcome == "reserved"
        user_id = db.append_prompt_send_user_message(
            session_id, send_id=send_id, holder=submit_holder, content="Question?")
        assert db.accept_prompt_send(
            session_id, send_id, {"status": "streaming"}, holder=submit_holder)
        assert db.acquire_session_turn_lease(session_id, first_lease, wait_seconds=0)
        assistant_id, answer = db.append_external_assistant_result(
            session_id, user_row_id=user_id, content="Answer.", turn_lease_holder=first_lease)
        assert answer == "Answer."
        # Replay/publish fails after the INSERT committed; discard the warm process.
        def unavailable(*args, **kwargs):
            raise OSError("post-commit replay unavailable")

        monkeypatch.setattr(db, "get_messages_as_conversation", unavailable)
        with pytest.raises(OSError, match="post-commit replay unavailable"):
            db.get_messages_as_conversation(session_id, include_row_ids=True)
        db.release_session_turn_lease(session_id, first_lease)
    finally:
        db.close()

    reopened = SessionDB(db_path=path)
    try:
        model, display = reopened.get_resume_conversations(session_id)
        for history in (model, display):
            assert [(row["role"], row["content"]) for row in history] == [
                ("user", "Question?"), ("assistant", "Answer.")]
            assert [row["_row_id"] for row in history] == [user_id, assistant_id]
        replay = reopened.reserve_prompt_send(
            session_id, send_id, "digest", holder="retry-submit")
        assert replay.outcome == "accepted"
        assert replay.result == {"status": "streaming"}  # Submit acceptance is not a terminal answer.
        assert reopened.acquire_session_turn_lease(session_id, retry_lease, wait_seconds=0)
        try:
            assert reopened.append_external_assistant_result(
                session_id, user_row_id=user_id, content="different retry",
                turn_lease_holder=retry_lease) == (assistant_id, "Answer.")
        finally:
            reopened.release_session_turn_lease(session_id, retry_lease)
        assert [(row["role"], row["content"]) for row in reopened.get_messages(session_id)] == [
            ("user", "Question?"), ("assistant", "Answer.")]
        stored_session = reopened.get_session(session_id)
        assert stored_session is not None
        assert stored_session["message_count"] == 2
    finally:
        reopened.close()
