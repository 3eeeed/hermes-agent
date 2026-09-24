"""Pure debate planning: injected calls, no gateway or session effects."""

import pytest

from tui_gateway.debate_choreography import DebateError, run_debate
from tui_gateway.prompt_execution_modes import DebateOptions


def options(rounds=2):
    return DebateOptions.model_validate({
        "side_a": {"provider": "provider-a", "model": "a"},
        "side_b": {"provider": "provider-b", "model": "b"},
        "judge": {"provider": "provider-j", "model": "j"},
        "rounds": rounds,
    }, strict=True)


def test_two_rounds_use_prior_opposition_only_and_independent_judge():
    calls = []

    def model_call(route, prompt, max_tokens):
        calls.append((route.model, prompt, max_tokens))
        return f"answer-{route.model}-{len(calls)}"

    answer = run_debate("How?", options(), model_call,
                        cancelled=lambda: False, deadline_exceeded=lambda: False)
    assert answer == "answer-j-5"
    assert [route for route, _, _ in calls] == ["a", "b", "a", "b", "j"]
    assert all("How?" in text for _, text, _ in calls)
    assert "answer-a-1" not in calls[1][1]
    assert "answer-a-1" in calls[3][1]
    assert "answer-b-2" in calls[2][1]
    assert "answer-a-3" not in calls[3][1]
    assert "answer-a-3" in calls[4][1]
    assert "answer-b-4" in calls[4][1]
    assert all(isinstance(tokens, int) and 0 < tokens <= 2048 for _, _, tokens in calls)


def test_opposing_context_keeps_latest_conclusion_within_bound():
    calls = []
    long_answer = "x" * 5000 + "LAST-CONCLUSION"

    def model_call(route, prompt, max_tokens):
        calls.append((route.model, prompt))
        return long_answer if len(calls) == 1 else "valid answer"

    run_debate("question", options(), model_call,
               cancelled=lambda: False, deadline_exceeded=lambda: False)
    second_round_b = calls[3][1]
    assert "LAST-CONCLUSION" in second_round_b
    assert len(second_round_b) < 9000


@pytest.mark.parametrize("rounds", [1, 2, 3])
def test_call_count_is_two_sides_per_round_plus_one_judge(rounds):
    calls = []

    def model_call(route, prompt, max_tokens):
        calls.append(route.model)
        return "answer"

    assert run_debate("question", options(rounds), model_call,
                      cancelled=lambda: False, deadline_exceeded=lambda: False) == "answer"
    assert calls == ["a", "b"] * rounds + ["j"]


@pytest.mark.parametrize("bad", ["", "  ", None, 3, "x" * 8001])
def test_rejects_invalid_user_prompt_without_model_calls(bad):
    with pytest.raises(DebateError):
        run_debate(bad, options(), lambda *args: pytest.fail("model was called"),
                   cancelled=lambda: False, deadline_exceeded=lambda: False)


@pytest.mark.parametrize("bad", ["", "  ", None, {"content": "no"}, "x" * 8001])
@pytest.mark.parametrize("failed_at", [1, 2, 3])
def test_rejects_invalid_side_and_judge_outputs_without_partial_result(bad, failed_at):
    calls = []

    def model_call(route, prompt, max_tokens):
        calls.append(route.model)
        return bad if len(calls) == failed_at else "valid"

    with pytest.raises(DebateError, match="Invalid debate model response"):
        run_debate("question", options(1), model_call,
                   cancelled=lambda: False, deadline_exceeded=lambda: False)
    assert len(calls) == failed_at


def test_model_failure_does_not_leak_provider_exception_or_continue():
    calls = []

    def model_call(route, prompt, max_tokens):
        calls.append(route.model)
        raise RuntimeError("credential-in-error")

    with pytest.raises(DebateError) as error:
        run_debate("question", options(), model_call,
                   cancelled=lambda: False, deadline_exceeded=lambda: False)
    assert "credential-in-error" not in str(error.value)
    assert calls == ["a"]


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_stop_signal_prevents_next_call_and_discards_partial_answer(stop):
    calls = []

    def model_call(route, prompt, max_tokens):
        calls.append(route.model)
        return "partial"

    def signalled(signal):
        return stop == signal and bool(calls)

    with pytest.raises(DebateError, match="cancelled|deadline"):
        run_debate("question", options(), model_call,
                   cancelled=lambda: signalled("cancel"),
                   deadline_exceeded=lambda: signalled("deadline"))
    assert calls == ["a"]


def test_route_fields_and_injected_credentials_never_enter_model_prompt():
    secret = "private-route-token"
    config = DebateOptions.model_validate({
        "side_a": {"provider": secret, "model": "private-model-a"},
        "side_b": {"provider": "b", "model": "private-model-b"},
        "judge": {"provider": "j", "model": "private-model-j"},
        "rounds": 1,
    }, strict=True)
    calls = []

    def model_call(route, prompt, max_tokens):
        calls.append((route, prompt))
        return "answer"

    run_debate("question", config, model_call,
               cancelled=lambda: False, deadline_exceeded=lambda: False)
    assert [route for route, _ in calls] == [config.side_a, config.side_b, config.judge]
    assert all(secret not in text and "private-model" not in text for _, text in calls)


def test_judge_and_side_prompts_are_bounded_for_three_maximal_rounds():
    prompts = []

    def model_call(route, prompt, max_tokens):
        prompts.append(prompt)
        return "x" * 8000

    assert len(run_debate("u" * 8000, options(3), model_call,
                          cancelled=lambda: False, deadline_exceeded=lambda: False)) == 8000
    assert len(prompts) == 7
    assert all(len(prompt) <= 24000 for prompt in prompts)
