"""Strict, read-only advisory model call for Debate/Delegate choreography.

The caller owns the profile runtime scope. No agent tools, auxiliary auto routing,
retry ladder, or cross-provider credential fallback participates in this call.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tui_gateway.prompt_execution_modes import Route


class ModeModelCallError(RuntimeError):
    """The selected advisory route could not produce a verified answer."""


_MAX_OUTPUT_CHARS = 8_000
ADVISORY_SYSTEM_PROMPT = ("You are one role inside the Hermes assistant's multi-model answer. Your text may be shown "
                         "to the user as Hermes' reply in an ongoing conversation: use the recent conversation, "
                         "answer in the user's language, and do not call tools or describe yourself or your limits.")
_TIMEOUT_SECONDS = 30.0


def _auxiliary_transport(**kwargs: Any) -> Any:
    """Use auxiliary's provider-aware client shims, without call_llm's recovery ladder."""
    from agent.auxiliary_client import resolve_provider_client

    client, resolved_model = resolve_provider_client(
        kwargs["provider"], model=kwargs["model"],
        explicit_base_url=kwargs["base_url"], explicit_api_key=kwargs["api_key"],
        api_mode=kwargs["api_mode"],
    )
    if client is None or resolved_model != kwargs["model"]:
        raise ModeModelCallError("Advisory transport did not resolve the selected model")
    return client.chat.completions.create(
        model=kwargs["model"],
        messages=[
            {"role": "system", "content": ADVISORY_SYSTEM_PROMPT},
            {"role": "user", "content": kwargs["prompt"]},
        ],
        max_tokens=kwargs["max_tokens"], timeout=kwargs["timeout"],
        tools=None,
        **({"extra_body": kwargs["extra_body"]} if kwargs["extra_body"] else {}),
    )


def call_model(
    route: Route, prompt: str, max_tokens: int, *,
    transport: Callable[..., Any] | None = None,
) -> str:
    """Call exactly the requested provider/model; raise on ambiguity or incomplete output.

    ``transport`` is the single test seam; production uses auxiliary client shims
    directly because ``call_llm`` may retry/fall back to another provider on error.
    The caller must bind the owning profile's home and secret scope before entry.
    """
    if (not isinstance(route, Route) or not route.provider.strip() or not route.model.strip()
            or route.provider != route.provider.strip() or route.model != route.model.strip()
            or route.provider.lower() in {"auto", "main", "moa"}
            or not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 24_000
            or type(max_tokens) is not int or not 1 <= max_tokens <= 2_048):
        raise ModeModelCallError("Invalid advisory call")
    if route.effort is not None:
        # The auxiliary adapters differ on reasoning-effort support. Never
        # silently ignore a role-specific setting or guess a provider wire key.
        raise ModeModelCallError("Advisory reasoning effort is not supported for this route")
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        runtime = resolve_runtime_provider(requested=route.provider, target_model=route.model)
        provider = runtime.get("provider")
        # Named custom entries resolve to the concrete 'custom' transport; all other
        # mismatches are an auto/fallback route, never permission to borrow its key.
        named_custom = (provider == "custom" and runtime.get("requested_provider") == route.provider
                        and route.provider not in {"custom", "openrouter"})
        if provider != route.provider and not named_custom:
            raise ModeModelCallError("Advisory provider did not resolve to selected route")
        if runtime.get("model") and runtime["model"] != route.model:
            raise ModeModelCallError("Advisory model did not resolve to selected route")
        if runtime.get("auth_error") or not runtime.get("api_key") or not runtime.get("base_url") or not runtime.get("api_mode"):
            raise ModeModelCallError("Advisory route is incomplete")
        response = (transport or _auxiliary_transport)(
            provider=route.provider, model=route.model, prompt=prompt,
            api_key=runtime["api_key"], base_url=runtime["base_url"],
            api_mode=runtime["api_mode"], max_tokens=max_tokens,
            timeout=_TIMEOUT_SECONDS, tools=None,
            extra_body=(runtime.get("request_overrides") or {}).get("extra_body"),
        )
        # Bedrock Converse commonly omits modelId in its response; the client
        # request above is already bound to the explicitly resolved route.
        response_model = response.model
        if (response_model != route.model and not (
                provider == "bedrock" and runtime["api_mode"] == "bedrock_converse"
                and response_model == ""
        )) or len(response.choices) != 1:
            raise ModeModelCallError("Advisory response route mismatch")
        choice = response.choices[0]
        content = choice.message.content
        if (choice.finish_reason != "stop" or choice.message.tool_calls
                or not isinstance(content, str) or not content.strip()
                or len(content) > _MAX_OUTPUT_CHARS):
            raise ModeModelCallError("Invalid advisory response")
        return content.strip()
    except Exception:
        # Avoid echoing transport/config exceptions: they may contain credentials.
        raise ModeModelCallError("Advisory model call failed") from None
