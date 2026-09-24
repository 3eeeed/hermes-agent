from types import SimpleNamespace
import pytest

from tui_gateway.delegate_readonly_lifecycle import run_readonly_delegate


def test_rejects_child_without_verifiable_tool_surface(monkeypatch):
    import tools.delegate_tool_results as results
    import hermes_cli.runtime_provider as providers
    monkeypatch.setattr(providers, "resolve_runtime_provider", lambda *, requested, target_model: {
        "provider": requested, "model": target_model, "api_key": "key", "base_url": "https://example/v1"})
    child = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(results, "_build_child_preserving_parent_tools", lambda **kw: child)
    with pytest.raises(ValueError, match="tool surface"):
        run_readonly_delegate(SimpleNamespace(), "q", {"workers": [{"lane": "a", "provider": "p", "model": "m"}],
                                                   "max_iterations": 1}, profile_home=None)
