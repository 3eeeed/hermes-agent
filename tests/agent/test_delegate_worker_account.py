"""A Delegate lane can be pinned to one account of its provider's credential pool."""
from types import SimpleNamespace

import pytest

from agent import delegate_orchestrator as orch
from tools.delegate_tool_child_run import _lease_child_credential
from tui_gateway.prompt_execution_modes import DelegateOptions

OPTIONS = {"workers": [{"lane": "code", "provider": "anthropic", "model": "m", "credential_id": "acc-2"}]}


class _Pool:
    def __init__(self, ids):
        self._entries = [SimpleNamespace(id=i) for i in ids]
        self.leased = []

    def entries(self):
        return self._entries

    def acquire_lease(self, credential_id=None):
        chosen = credential_id or self._entries[0].id
        self.leased.append(chosen)
        self._current = chosen
        return chosen

    def current(self):
        return next(e for e in self._entries if e.id == self._current)


def test_saved_settings_accept_an_account_and_the_lane_route_carries_it():
    DelegateOptions.model_validate(OPTIONS)  # older saved settings without it still load
    DelegateOptions.model_validate({"workers": [{"lane": "code", "provider": "anthropic", "model": "m"}]})
    assert orch.lane_routes(OPTIONS)["code"]["credential_id"] == "acc-2"
    assert "credential_id" not in orch.lane_routes({"workers": [{"lane": "x", "provider": "p", "model": "m"}]})["x"]


def test_the_worker_leases_exactly_the_chosen_account():
    pool = _Pool(["acc-1", "acc-2"])
    swapped = []
    child = SimpleNamespace(_credential_pool=pool, _delegate_credential_id="acc-2",
                            _swap_credential=lambda entry: swapped.append(entry.id))
    assert _lease_child_credential(child) == (pool, "acc-2")
    assert swapped == ["acc-2"]

    unpinned = SimpleNamespace(_credential_pool=pool, _swap_credential=lambda entry: None)
    assert _lease_child_credential(unpinned)[1] == "acc-1"  # no pin: normal least-leased choice


@pytest.mark.parametrize("registered, expect_error", [(["acc-1", "acc-2"], False), (["acc-1"], True)])
def test_a_removed_account_is_refused_before_any_worker_starts(monkeypatch, registered, expect_error):
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: _Pool(registered))
    agent = SimpleNamespace(_delegate_lane_routes=orch.lane_routes(OPTIONS), _delegate_default_lane="code")
    route, error = orch.routing_for_tasks(agent, [{"goal": "g"}])
    if expect_error:
        assert route is None and "no longer registered" in error
    else:
        assert error is None and route["credential_id"] == "acc-2"
