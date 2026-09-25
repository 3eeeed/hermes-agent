"""Disabled-admission Delegate runtime: real children, never advisory completion."""
from types import SimpleNamespace

import pytest

from tui_gateway.delegate_readonly_lifecycle import run_readonly_delegate


def test_stop_during_inflight_synthesis_discards_answer(monkeypatch):
    import threading
    import hermes_cli.runtime_provider as providers
    import tui_gateway.delegate_readonly_lifecycle as lifecycle
    monkeypatch.setattr(providers, "resolve_runtime_provider", lambda *, requested, target_model: {
        "provider": requested, "model": target_model, "base_url": "https://example.invalid/v1", "api_key": "fake"})
    monkeypatch.setattr(lifecycle, "run_readonly_delegate", lambda *a, **k: [
        {"status": "completed", "summary": "evidence"}])
    entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
    options = {"orchestrator": {"provider": "x", "model": "judge"},
               "workers": [{"lane": "research", "provider": "x", "model": "worker"}],
               "max_iterations": 1, "permissions": "read-only"}
    outcome = []
    def synth(*args):
        entered.set()
        assert release.wait(3)
        return "Answer that must not be committed"
    def task():
        try:
            outcome.append(lifecycle.run_readonly_delegate_answer(
                SimpleNamespace(), "Question?", options, profile_home=None,
                synthesize=synth, cancelled=cancel.is_set))
        except RuntimeError as exc:
            outcome.append(str(exc))
    thread = threading.Thread(target=task)
    thread.start()
    assert entered.wait(3)
    cancel.set()
    release.set()
    thread.join(4)
    assert not thread.is_alive()
    assert len(outcome) == 1 and "cancelled" in outcome[0].lower()


def test_stop_closes_queued_child_that_never_started(monkeypatch):
    import threading
    import tools.delegate_tool_results as results
    import hermes_cli.runtime_provider as providers
    monkeypatch.setattr(providers, "resolve_runtime_provider", lambda *, requested, target_model: {
        "provider": requested, "model": target_model, "base_url": "https://example.invalid/v1", "api_key": "fake"})
    cancel, started, stopped = threading.Event(), threading.Event(), threading.Event()
    children, closed, called = [], [], []
    def build(**kw):
        i = kw["task_index"]
        child = SimpleNamespace(valid_tool_names={"web_search"},
                                interrupt=lambda: stopped.set() if i == 0 else None,
                                close=lambda: closed.append(i))
        children.append(child)
        return child
    def run(index, goal, child, parent):
        called.append(index)
        started.set()
        assert stopped.wait(3)
        return {"status": "completed", "summary": "late"}
    monkeypatch.setattr(results, "_build_child_preserving_parent_tools", build)
    monkeypatch.setattr(results, "_run_child_lifecycle", run)
    options = {"workers": [{"lane": str(i), "provider": "x", "model": "m"} for i in range(2)],
               "max_iterations": 1, "max_concurrency": 1}
    errors = []
    def task():
        try:
            run_readonly_delegate(SimpleNamespace(), "question", options, profile_home=None,
                                  cancelled=cancel.is_set)
        except RuntimeError as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=task)
    thread.start()
    assert started.wait(3)
    cancel.set()
    thread.join(4)
    assert not thread.is_alive()
    assert errors and "cancelled" in errors[0].lower()
    assert called == [0]
    assert closed == [1]


def test_stop_during_children_interrupts_and_never_synthesizes(monkeypatch):
    import threading
    import tools.delegate_tool_results as results
    import hermes_cli.runtime_provider as providers
    from tui_gateway.delegate_readonly_lifecycle import run_readonly_delegate_answer
    monkeypatch.setattr(providers, "resolve_runtime_provider", lambda *, requested, target_model: {
        "provider": requested, "model": target_model, "base_url": "https://example.invalid/v1", "api_key": "fake"})
    started, stopped = threading.Event(), threading.Event()
    cancel = threading.Event()
    syntheses, interrupted = [], []
    def build(**kw):
        def interrupt():
            interrupted.append(kw["task_index"])
            stopped.set()
        return SimpleNamespace(valid_tool_names={"web_search", "web_extract"},
                               interrupt=interrupt, close=lambda: None)
    def run(index, goal, child, parent):
        started.set()
        assert stopped.wait(3), "Stop did not interrupt the live child"
        return {"status": "completed", "summary": "late result"}
    monkeypatch.setattr(results, "_build_child_preserving_parent_tools", build)
    monkeypatch.setattr(results, "_run_child_lifecycle", run)
    options = {"orchestrator": {"provider": "x", "model": "judge"},
               "workers": [{"lane": "research", "provider": "x", "model": "worker"}],
               "max_iterations": 2, "permissions": "read-only"}
    outcome = []
    def task():
        try:
            run_readonly_delegate_answer(SimpleNamespace(), "Question?", options, profile_home=None,
                                         synthesize=lambda *a: syntheses.append(1) or "late answer",
                                         cancelled=cancel.is_set)
        except RuntimeError as exc:
            outcome.append(str(exc))
    thread = threading.Thread(target=task)
    thread.start()
    assert started.wait(3)
    cancel.set()
    thread.join(4)
    assert not thread.is_alive()
    assert outcome and "cancelled" in outcome[0].lower()
    assert interrupted == [0]
    assert syntheses == []


def test_rejects_search_only_worker_before_child_construction(monkeypatch):
    import hermes_cli.runtime_provider as providers
    from tui_gateway.prompt_execution_modes import admission_error
    monkeypatch.setattr(providers, "resolve_runtime_provider", lambda **kw: {
        "provider": "x", "model": "m", "api_key": "fake", "base_url": "https://example/v1"})
    options = {"orchestrator": {"provider": "x", "model": "m"},
               "workers": [{"lane": "research", "provider": "x", "model": "m", "toolsets": ["search"]}],
               "max_iterations": 1, "permissions": "read-only"}
    built = []
    import tools.delegate_tool_results as results
    monkeypatch.setattr(results, "_build_child_preserving_parent_tools", lambda **kw: built.append(kw))
    assert admission_error("delegate", options) is not None
    with pytest.raises(ValueError, match="toolset"):
        run_readonly_delegate(SimpleNamespace(), "question", options, profile_home=None)
    assert built == []


def test_selected_orchestrator_synthesizes_only_bounded_child_results(monkeypatch):
    import tools.delegate_tool_results as results
    import hermes_cli.runtime_provider as providers
    monkeypatch.setattr(providers, "resolve_runtime_provider", lambda *, requested, target_model: {
        "provider": requested, "model": target_model,
        "api_key": "private-key", "base_url": "https://provider.example/v1",
        "api_mode": "chat_completions"})
    monkeypatch.setattr(results, "_build_child_preserving_parent_tools", lambda **kw: SimpleNamespace(
        valid_tool_names={"web_search"}, close=lambda: None, route=kw["override_provider"]))
    monkeypatch.setattr(results, "_run_child_lifecycle", lambda i, goal, child, parent:
                        {"task_index": i, "status": "completed", "summary": child.route + " finding"})
    from tui_gateway.delegate_readonly_lifecycle import run_readonly_delegate_answer
    seen = []
    def synthesize(route, prompt, max_tokens):
        seen.append((route.provider, route.model, prompt, max_tokens))
        return "Final delegated answer"
    options = {"orchestrator": {"provider": "judge", "model": "judge-model"},
               "workers": [{"lane": "alpha", "provider": "a", "model": "m"},
                           {"lane": "beta", "provider": "b", "model": "m"}],
               "max_iterations": 2, "max_concurrency": 2, "permissions": "read-only"}
    answer, children = run_readonly_delegate_answer(
        SimpleNamespace(), "question", options, profile_home=None, synthesize=synthesize)
    assert answer == "Final delegated answer"
    assert [c["summary"] for c in children] == ["a finding", "b finding"]
    assert seen[0][0:2] == ("judge", "judge-model")
    assert "a finding" in seen[0][2] and "b finding" in seen[0][2]
    assert "private-key" not in seen[0][2]


def test_rejects_missing_profile_route_before_child_construction(monkeypatch):
    import tools.delegate_tool_results as results
    monkeypatch.setattr(results, "_build_child_preserving_parent_tools", lambda **kw: pytest.fail("child built"))
    with pytest.raises(ValueError, match="credential|provider"):
        run_readonly_delegate(SimpleNamespace(), "question", {"workers": [{"lane": "a", "provider": "missing", "model": "m"}], "max_iterations": 2}, profile_home=None)


def test_distinct_real_children_bounded_and_collected(monkeypatch):
    import tools.delegate_tool_results as results
    import hermes_cli.runtime_provider as providers
    import tui_gateway.delegate_readonly_lifecycle as lifecycle
    monkeypatch.setattr(providers, "resolve_runtime_provider", lambda *, requested, target_model: {
        "provider": requested, "model": target_model, "base_url": f"https://{requested}.example/v1",
        "api_key": f"key-{requested}", "api_mode": "chat_completions",
    })
    created = []
    active = 0
    maximum = 0
    from threading import Lock
    lock = Lock()
    def build(**kwargs):
        assert kwargs["strict_route"] is True
        assert kwargs["force_leaf"] is True
        assert kwargs["toolsets"] == ["web"]
        assert kwargs["routing_cfg"] == {"fallback_providers": []}
        assert kwargs["max_iterations"] == 3
        child = SimpleNamespace(route=kwargs["override_provider"], close=lambda: None,
                                valid_tool_names={"web_search", "web_extract"})
        created.append(child)
        return child
    def run(index, goal, child, parent):
        nonlocal active, maximum
        import time
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(.02)
        with lock:
            active -= 1
        return {"task_index": index, "status": "completed", "summary": child.route}
    monkeypatch.setattr(results, "_build_child_preserving_parent_tools", build)
    monkeypatch.setattr(results, "_run_child_lifecycle", run)
    options = {"workers": [{"lane": str(i), "provider": f"p{i}", "model": "m", "toolsets": ["web"]} for i in range(4)],
               "max_iterations": 3, "max_concurrency": 2, "permissions": "read-only"}
    outputs = run_readonly_delegate(SimpleNamespace(), "question", options, profile_home=None)
    assert len({id(c) for c in created}) == 4
    assert [r["summary"] for r in outputs] == ["p0", "p1", "p2", "p3"]
    assert maximum == 2


def test_rejects_fallback_or_mutating_toolset(monkeypatch):
    import hermes_cli.runtime_provider as providers
    monkeypatch.setattr(providers, "resolve_runtime_provider", lambda *, requested, target_model: {
        "provider": requested, "model": target_model, "api_key": "key", "base_url": "https://example/v1"})
    base = {"workers": [{"lane": "a", "provider": "x", "model": "m"}], "max_iterations": 1}
    for change in ({"permissions": "write"}, {"workers": [{**base["workers"][0], "toolsets": ["file"]}]},
                   {"max_iterations": 0}, {"orchestrator": {"provider": "x"}}):
        with pytest.raises(ValueError):
            run_readonly_delegate(SimpleNamespace(), "question", {**base, **change}, profile_home=None)


def test_strict_builder_never_borrows_parent_key_pool_or_orchestrator(monkeypatch):
    import tools.delegate_tool as delegate
    from unittest.mock import MagicMock
    parent = SimpleNamespace(model="parent", provider="same", base_url="https://parent", api_key="parent-key",
                             _credential_pool=object(), enabled_toolsets=["web"], disabled_toolsets=[],
                             request_overrides={}, session_id=None, _delegate_depth=0, _client_kwargs={})
    child = MagicMock()
    child.valid_tool_names = {"web_search", "web_extract"}
    monkeypatch.setattr("run_agent.AIAgent", MagicMock(return_value=child))
    monkeypatch.setattr(delegate, "_get_orchestrator_enabled", lambda: True)
    monkeypatch.setattr(delegate, "_get_max_spawn_depth", lambda: 5)
    monkeypatch.setattr(delegate, "_resolve_child_credential_pool", lambda *a: pytest.fail("borrowed pool"))
    monkeypatch.setattr(delegate, "_load_config", lambda: {})
    monkeypatch.setattr(delegate, "_attach_child", lambda *a: None)
    monkeypatch.setattr(delegate, "_build_child_progress_callback", lambda *a, **kw: None)
    built = delegate._build_child_agent(0, "research", None, ["web"], "m", 2, 1, parent,
                                       override_provider="same", override_base_url="https://same",
                                       override_api_key="scoped-key", routing_cfg={"fallback_providers": []},
                                       strict_route=True, force_leaf=True)
    assert built._credential_pool is None
    assert built._delegate_role == "leaf"
    kwargs = __import__("run_agent").AIAgent.call_args.kwargs
    assert kwargs["api_key"] == "scoped-key"
    assert kwargs["fallback_model"] is None
    assert "delegation" not in kwargs["enabled_toolsets"]


def test_strict_builder_refuses_orchestrator_role_without_force_leaf(monkeypatch):
    import tools.delegate_tool as delegate
    parent = SimpleNamespace(enabled_toolsets=["web"], disabled_toolsets=[], _delegate_depth=0)
    monkeypatch.setattr(delegate, "_load_config", lambda: {})
    with pytest.raises(ValueError, match="leaf"):
        delegate._build_child_agent(0, "research", None, ["web"], "m", 2, 1, parent,
                                    override_provider="p", override_base_url="https://p", override_api_key="key",
                                    routing_cfg={"fallback_providers": []}, strict_route=True)


def test_fails_closed_if_constructed_child_has_mutating_tool(monkeypatch):
    import tools.delegate_tool_results as results
    import hermes_cli.runtime_provider as providers
    monkeypatch.setattr(providers, "resolve_runtime_provider", lambda *, requested, target_model: {
        "provider": requested, "model": target_model, "api_key": "key", "base_url": "https://example/v1"})
    closed = []
    monkeypatch.setattr(results, "_build_child_preserving_parent_tools", lambda **kw: SimpleNamespace(
        valid_tool_names={"web_search", "write_file"}, close=lambda: closed.append(True)))
    monkeypatch.setattr(results, "_run_child_lifecycle", lambda *a: pytest.fail("unsafe child ran"))
    with pytest.raises(ValueError, match="read-only allowlist"):
        run_readonly_delegate(SimpleNamespace(), "q", {"workers": [{"lane": "a", "provider": "p", "model": "m"}],
                                                   "max_iterations": 1}, profile_home=None)
    assert closed == [True]


def test_rejects_resolved_route_other_than_requested_provider(monkeypatch):
    import hermes_cli.runtime_provider as providers
    monkeypatch.setattr(providers, "resolve_runtime_provider", lambda *, requested, target_model: {
        "provider": "parent", "model": target_model, "api_key": "key", "base_url": "https://example/v1"})
    with pytest.raises(ValueError, match="verified profile credential route"):
        run_readonly_delegate(SimpleNamespace(), "q", {"workers": [{"lane": "a", "provider": "p", "model": "m"}],
                                                   "max_iterations": 1}, profile_home=None)
