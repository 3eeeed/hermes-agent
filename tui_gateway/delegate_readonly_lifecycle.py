"""Gateway-only, disabled-admission read-only Delegate runner.

Not wired into prompt admission. Each worker is a real AIAgent child using the
existing delegation lifecycle; this module never substitutes a model completion.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy


_READ_TOOLS = frozenset({"web_search", "web_extract"})


def _validate(options):
    if not isinstance(options, dict) or options.get("permissions", "read-only") != "read-only":
        raise ValueError("Delegate requires read-only permissions")
    if options.get("orchestrator") is not None:
        raise ValueError("Delegate orchestrator route is not implemented; refusing instead of silently ignoring it")
    workers = options.get("workers")
    if not isinstance(workers, list) or not 1 <= len(workers) <= 4:
        raise ValueError("Delegate requires 1–4 workers")
    iterations = options.get("max_iterations")
    concurrency = options.get("max_concurrency", 1)
    if type(iterations) is not int or not 1 <= iterations <= 20:
        raise ValueError("Delegate max_iterations must be 1–20")
    if type(concurrency) is not int or not 1 <= concurrency <= 4:
        raise ValueError("Delegate max_concurrency must be 1–4")
    for worker in workers:
        if not isinstance(worker, dict) or not all(isinstance(worker.get(k), str) and worker[k].strip()
                                                    for k in ("lane", "provider", "model")):
            raise ValueError("Each worker requires a lane, provider and model")
        requested = worker.get("toolsets")
        if requested is not None and (not isinstance(requested, list) or
                                      not set(map(str, requested)) <= {"safe", "search", "web"}):
            raise ValueError("Only read-only Delegate toolsets are permitted")
    return workers, iterations, concurrency


def _profile_scope(profile_home):
    from tui_gateway.server import _session_profile_runtime_scope
    return _session_profile_runtime_scope({"profile_home": profile_home})


def _route(worker):
    from hermes_cli.runtime_provider import resolve_runtime_provider
    provider, model = worker["provider"].strip(), worker["model"].strip()
    try:
        runtime = resolve_runtime_provider(requested=provider, target_model=model)
    except Exception as exc:
        raise ValueError(f"Delegate provider {provider!r} unavailable in profile") from exc
    # Reject aliases/custom endpoints and transport commands until their identity
    # and isolation can be verified. Never inherit parent keys or fallback chains.
    if (not isinstance(runtime, dict) or runtime.get("provider") != provider or
            runtime.get("model") not in (None, model) or not runtime.get("api_key") or
            not runtime.get("base_url") or runtime.get("command") or provider == "custom"):
        raise ValueError(f"Delegate provider/model {provider!r}/{model!r} has no verified profile credential route")
    return runtime


def run_readonly_delegate(parent_agent, prompt: str, options: dict, *, profile_home: str | None):
    """Build isolated children before dispatch, run at most max_concurrency, return ordered results.

    Admission stays disabled elsewhere. Failure to preflight any worker builds no
    children; once built, each child has its own lifecycle and failure result.
    """
    from tools.delegate_tool_results import _build_child_preserving_parent_tools, _run_child_lifecycle
    workers, iterations, concurrency = _validate(options)
    routes = []
    with _profile_scope(profile_home):
        for worker in workers:
            routes.append(_route(worker))
        children = []
        try:
            for index, (worker, route) in enumerate(zip(workers, routes)):
                child = _build_child_preserving_parent_tools(
                    task_index=index, task_count=len(workers), goal=f"{worker['lane']}: {prompt}",
                    context="Read-only research. Do not mutate files, accounts, services, or state.",
                    toolsets=["web"], model=worker["model"], max_iterations=iterations,
                    parent_agent=parent_agent, override_provider=worker["provider"],
                    override_base_url=route["base_url"], override_api_key=route["api_key"],
                    override_api_mode=route.get("api_mode"),
                    override_request_overrides=deepcopy(route.get("request_overrides") or {}),
                    routing_cfg={"fallback_providers": []}, strict_route=True, force_leaf=True,
                )
                children.append(child)
                names = getattr(child, "valid_tool_names", None)
                if not isinstance(names, set):
                    raise ValueError("Delegate child has no verifiable tool surface")
                if not names <= _READ_TOOLS:
                    raise ValueError("Delegate child acquired tools outside the read-only allowlist")
        except BaseException:
            for child in children:
                child.close()
            raise

    def run(index, child):
        with _profile_scope(profile_home):
            return _run_child_lifecycle(index, f"{workers[index]['lane']}: {prompt}", child, parent_agent)

    with ThreadPoolExecutor(max_workers=min(concurrency, len(children)), thread_name_prefix="delegate-readonly") as executor:
        futures = {executor.submit(run, i, child): i for i, child in enumerate(children)}
        output: list[dict | None] = [None] * len(children)
        for future in as_completed(futures):
            output[futures[future]] = future.result()
    return output
