"""Human commands owned by the LLM plugin.

``/provider``, ``/model``, and ``/effort`` are registered by the LLM
component itself; handlers resolve the domain services (agents selection,
LLM catalog) at runtime.  ``/reload`` is a session command (system soft
restart).
"""

from __future__ import annotations

from typing import Any

from XBotv2.core.commands import (
    Command,
    CommandResult,
    command_usage,
    guard_command,
    run_command_operation,
    split_command_args,
)


async def provider_command(ctx: Any, raw_args: str) -> CommandResult:
    parts = split_command_args(raw_args)
    action = parts[0].lower() if parts else "status"
    if action == "status" and len(parts) <= 1:
        data = _model_status(ctx)
        return CommandResult(
            f"Provider: {data['provider']} ({data['model']})", data=data
        )
    if action == "list" and len(parts) == 1:
        return _provider_list(ctx)
    if action == "use" and len(parts) == 2:
        return await _select_provider(
            ctx,
            parts[1],
            None,
            "Provider switched to",
        )
    return command_usage("/provider [status|list|use <name>]")


async def model_command(ctx: Any, raw_args: str) -> CommandResult:
    parts = split_command_args(raw_args)
    action = parts[0].lower() if parts else "status"
    if action == "status" and len(parts) <= 1:
        data = _model_status(ctx)
        return CommandResult(
            f"Model: {data['provider']} ({data['model']})", data=data
        )
    if action == "list" and len(parts) == 1:
        listed = _provider_list(ctx)
        current = ctx.engine.settings.model
        data = dict(listed.data or {})
        data["current_model"] = current
        lines = []
        for item in data.get("providers") or []:
            models = ", ".join(
                f"{model['model']}{'*' if model['model'] == current else ''}"
                for model in item["models"]
            )
            lines.append(f"{item['name']}: {models}")
        return CommandResult("Models: " + " | ".join(lines), data=data)
    if action == "use":
        rest = [part for part in parts[1:] if part]
        if len(rest) == 1:
            provider = ctx.engine.settings.provider
            model = rest[0]
        elif len(rest) == 2:
            provider, model = rest
        else:
            return command_usage("/model use [<provider>] <model>")
        return await _select_provider(
            ctx,
            provider,
            model,
            "Model switched to",
        )
    return command_usage("/model [status|list|use [<provider>] <model>]")


async def effort_command(ctx: Any, raw_args: str) -> CommandResult:
    parts = split_command_args(raw_args)
    if not parts:
        settings = ctx.engine.settings
        tiers: list[str] = []
        llm = ctx.services.get("llm")
        if llm is not None:
            try:
                entry = llm.provider_config(
                    settings.provider, require_key=False
                )
                model_config = entry.resolve(settings.model)
                tiers = list(model_config.effort or [])
            except Exception:  # noqa: BLE001 - display falls back to no tiers
                tiers = []
        message = f"Effort: {settings.model_mode or 'default'}"
        message += (
            " (" + ", ".join(tiers) + ")"
            if tiers
            else "; model advertises no effort tiers"
        )
        return CommandResult(
            message,
            data={
                "provider": settings.provider,
                "model": settings.model,
                "model_mode": settings.model_mode,
                "effort": tiers,
            },
        )
    if len(parts) == 1:
        return await _select_effort(
            ctx,
            parts[0],
        )
    return command_usage("/effort [<level>]")


async def _select_provider(
    ctx: Any,
    name: str,
    model: str | None,
    verb: str,
) -> CommandResult:
    if ctx.turn_lock.locked():
        return CommandResult(
            "Cannot switch provider while a turn is active.",
            status="error",
            data={"code": "thread_busy"},
        )
    async with ctx.turn_lock:
        try:
            selected = await ctx.services.agents.select_provider(name, model=model)
        except ValueError as error:
            return CommandResult(
                str(error), status="error", data={"code": "selection_failed"}
            )
        ctx.provider_name = selected["provider"]
    return CommandResult(
        f"{verb} {selected['provider']} ({selected['model']}).",
        data=selected,
    )


async def _select_effort(ctx: Any, level: str) -> CommandResult:
    if ctx.turn_lock.locked():
        return CommandResult(
            "Cannot switch effort while a turn is active.",
            status="error",
            data={"code": "thread_busy"},
        )
    async with ctx.turn_lock:
        try:
            selected = await ctx.services.agents.select_effort(level)
        except ValueError as error:
            return CommandResult(
                str(error), status="error", data={"code": "unsupported_effort"}
            )
        ctx.provider_name = selected["provider"]
    return CommandResult(
        f"Effort switched to {selected['reasoning_effort']} "
        f"({selected['model_mode']}).",
        data=selected,
    )


LLM_COMMANDS: tuple[Command, ...] = (
    Command(
        name="provider",
        description="List or switch provider configuration",
        handler=guard_command(provider_command),
        usage="/provider [status|list|use <name>]",
        examples=("/provider list", "/provider use minimax"),
    ),
    Command(
        name="model",
        description="List or switch the model within a provider",
        handler=guard_command(model_command),
        usage="/model [status|list|use [<provider>] <model>]",
        examples=("/model list", "/model use Minimax-M3"),
    ),
    Command(
        name="effort",
        description="Show or switch the reasoning effort tier",
        handler=guard_command(effort_command),
        usage="/effort [<level>]",
    ),
)


def _model_status(ctx: Any) -> dict[str, Any]:
    settings = ctx.engine.settings
    return {
        "provider": settings.provider,
        "model": settings.model,
        "model_mode": settings.model_mode,
        "context_window": ctx.engine.context_window,
    }


def _provider_list(ctx: Any) -> CommandResult:
    llm = ctx.services.get("llm")
    if llm is None:
        return CommandResult(
            "LLM plugin is not loaded.", status="error", data={"code": "unavailable"}
        )
    current = ctx.engine.settings.provider
    providers = []
    for name in llm.names():
        entry = llm.provider_config(name, require_key=False)
        providers.append({
            "name": name,
            "provider": entry.protocol,
            "default_model": entry.default_model,
            "models": [
                {
                    "model": model.model,
                    "max_context_tokens": model.max_context_tokens,
                    "max_output_tokens": model.max_output_tokens,
                    "reasoning_effort": model.reasoning_effort or "",
                    "effort": list(model.effort or []),
                    "thinking": model.thinking or "",
                    "input_modalities": model.input_modalities,
                }
                for model in entry.models
            ],
        })
    return CommandResult(
        "Providers: " + ", ".join(
            f"{item['name']}{' (current)' if item['name'] == current else ''}"
            for item in providers
        ),
        data={"providers": providers, "current": current},
    )


__all__ = ["LLM_COMMANDS", "provider_command", "model_command", "effort_command"]
