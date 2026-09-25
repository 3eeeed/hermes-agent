"""Per-message mode intent must not become an unverified ordinary agent turn."""
import threading
from types import SimpleNamespace

import pytest

from tui_gateway import server
from tui_gateway.prompt_execution_modes import admission_error


@pytest.fixture
def session(monkeypatch):
    record = {
        "agent": SimpleNamespace(session_id="mode-stored", clear_interrupt=lambda: None),
        "session_key": "mode-stored", "_sid": "mode-runtime", "history": [],
        "history_lock": threading.RLock(), "history_version": 0, "running": False,
        "attached_images": [], "cols": 80, "source": "desktop", "slash_worker": None,
        "inflight_turn": None, "active_session_lease": None,
    }
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a, **k: True)
    monkeypatch.setattr(server, "_persist_session_row_for_submit", lambda *a, **k: None)
    with server._sessions_lock:
        server._sessions["mode-runtime"] = record
    yield record
    with server._sessions_lock:
        server._sessions.pop("mode-runtime", None)


def submit(**kwargs):
    return server.handle_request({"id": 1, "method": "prompt.submit", "params": {
        "session_id": "mode-runtime", "text": "question", **kwargs}})


@pytest.mark.parametrize("mode,options", [
    ("delegate", {"workers": [{"lane": "research", "provider": "openai", "model": "gpt-test"}],
                  "max_iterations": 2}),
    ("debate", {"side_a": {"provider": "openai", "model": "gpt-test"},
                 "side_b": {"provider": "openai", "model": "gpt-test"},
                 "judge": {"provider": "openai", "model": "gpt-test"}, "rounds": 1}),
])
def test_unavailable_native_modes_refuse_before_turn_or_history_mutation(session, mode, options, monkeypatch):
    # Isolated compute hosts: Debate needs a parent lease, Delegate is not offered there.
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: True)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    before = (list(session["history"]), session["running"], session["inflight_turn"])
    result = submit(execution_mode=mode, mode_options=options)
    assert result["error"]["message"].lower().find(mode) >= 0
    assert (session["history"], session["running"], session["inflight_turn"]) == before


@pytest.mark.parametrize("mode,options", [
    ("delegate", {"workers": [], "max_iterations": 2}),
    ("delegate", {"workers": [{"lane": "x", "provider": "p", "model": "m"}], "max_iterations": 501}),
    ("debate", {"side_a": {"provider": "p", "model": "m"}, "side_b": {"provider": "p", "model": "m"},
                 "judge": {"provider": "p", "model": "m"}, "rounds": 4}),
])
def test_invalid_mode_options_are_rejected_before_admission(session, mode, options):
    result = submit(execution_mode=mode, mode_options=options)
    assert result["error"]
    assert session["running"] is False


def test_normal_rejects_unconsumed_mode_options(session):
    result = submit(execution_mode="normal", mode_options={"workers": []})
    assert result["error"]
    assert session["running"] is False


def test_invalid_options_do_not_echo_private_values(session):
    secret = "not-for-response-credential-123"
    result = submit(execution_mode="debate", mode_options={"judge": {"provider": secret}})
    assert secret not in result["error"]["message"]


def test_gateway_advertises_verified_native_modes():
    response = server.handle_request({"id": 6, "method": "gateway.capabilities", "params": {}})
    assert response["result"]["execution_modes"] == ["normal", "delegate", "debate"]


def test_saved_desktop_delegate_settings_use_the_backend_contract(session):
    options = {
        "orchestrator": {"provider": "openai-codex", "model": "gpt-test", "effort": "xhigh"},
        "workers": [{"lane": "research", "provider": "openai-codex", "model": "gpt-test"}],
        "max_iterations": 2, "max_concurrency": 1, "permissions": "read-only",
    }
    # Settings saved by the earlier editor (orchestrator route, per-role effort) keep working:
    # a chosen effort is honoured, never a reason to lock the mode.
    assert admission_error("delegate", options) is None
    duplicate = {"workers": [{"lane": "a", "provider": "p", "model": "m"}] * 2}
    assert admission_error("delegate", duplicate) == "Delegate lanes must have unique names"


def test_isolated_delegate_without_lease_refuses_before_durable_acceptance(session, monkeypatch):
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: True)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    staged = []
    monkeypatch.setattr(server, "_persist_submit_user_row", lambda *a, **k: staged.append(1))
    options = {"orchestrator": {"provider": "openrouter", "model": "judge"},
               "workers": [{"lane": "research", "provider": "openrouter", "model": "worker"}],
               "max_iterations": 2, "max_concurrency": 1, "permissions": "read-only"}
    from tui_gateway.compute_host_bridge import _active_session_lease_vouch
    assert _active_session_lease_vouch(session) is None
    response = submit(execution_mode="delegate", mode_options=options, client_send_id="no-lease")
    assert response["error"]["code"] == 4126
    assert staged == []
    assert session["running"] is False
    assert session["history"] == []


def test_mode_intent_changes_send_identity():
    normal = {"session_id": "one", "text": "question", "client_send_id": "same"}
    delegate = {**normal, "execution_mode": "delegate", "mode_options": {"workers": []}}
    assert server._send_payload_digest(normal) != server._send_payload_digest(delegate)
    assert server._send_payload_digest(delegate) != server._send_payload_digest({**delegate, "mode_options": {"workers": [1]}})
