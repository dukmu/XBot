"""Human LLM commands and their typed service binding factory."""

from __future__ import annotations

from XBotv2.agents.services import AgentRuntimePort
from XBotv2.commands import (
    Command,
    CommandResult,
    command_usage,
    guard_command,
    split_command_args,
)
from XBotv2.llm.services import LlmCatalogPort


def build_llm_commands(
    runtime: AgentRuntimePort,
    llm: LlmCatalogPort,
) -> tuple[Command, ...]:
    def provider_list() -> CommandResult:
        selected = runtime.current_selection()
        catalog = llm.catalog()
        return CommandResult(
            "Providers: " + ", ".join(
                f"{item.name}{' (current)' if item.name == selected.provider else ''}"
                for item in catalog.providers
            )
        )

    async def select_provider(
        name: str,
        model: str | None,
        verb: str,
    ) -> CommandResult:
        selected = await runtime.select_provider(name, model=model)
        return CommandResult(
            f"{verb} {selected['provider']} ({selected['model']})."
        )

    async def provider_command(raw_args: str) -> CommandResult:
        parts = split_command_args(raw_args)
        action = parts[0].lower() if parts else "status"
        if action == "status" and len(parts) <= 1:
            selected = runtime.current_selection()
            return CommandResult(
                f"Provider: {selected.provider} ({selected.model})"
            )
        if action == "list" and len(parts) == 1:
            return provider_list()
        if action == "use" and len(parts) == 2:
            return await select_provider(parts[1], None, "Provider switched to")
        return command_usage("/provider [status|list|use <name>]")

    async def model_command(raw_args: str) -> CommandResult:
        parts = split_command_args(raw_args)
        action = parts[0].lower() if parts else "status"
        selected = runtime.current_selection()
        if action == "status" and len(parts) <= 1:
            return CommandResult(f"Model: {selected.provider} ({selected.model})")
        if action == "list" and len(parts) == 1:
            lines = []
            for provider in llm.catalog().providers:
                models = ", ".join(
                    f"{model.model}{'*' if model.model == selected.model else ''}"
                    for model in provider.models
                )
                lines.append(f"{provider.name}: {models}")
            return CommandResult("Models: " + " | ".join(lines))
        if action == "use":
            rest = [part for part in parts[1:] if part]
            if len(rest) == 1:
                provider, model = selected.provider, rest[0]
            elif len(rest) == 2:
                provider, model = rest
            else:
                return command_usage("/model use [<provider>] <model>")
            return await select_provider(provider, model, "Model switched to")
        return command_usage("/model [status|list|use [<provider>] <model>]")

    async def effort_command(raw_args: str) -> CommandResult:
        parts = split_command_args(raw_args)
        selected = runtime.current_selection()
        if not parts:
            tiers = next(
                (
                    model.effort
                    for provider in llm.catalog().providers
                    if provider.name == selected.provider
                    for model in provider.models
                    if model.model == selected.model
                ),
                (),
            )
            message = f"Effort: {selected.model_mode or 'default'}"
            message += (
                " (" + ", ".join(tiers) + ")"
                if tiers
                else "; model advertises no effort tiers"
            )
            return CommandResult(message)
        if len(parts) == 1:
            changed = await runtime.select_effort(parts[0])
            return CommandResult(
                f"Effort switched to {changed['reasoning_effort']} "
                f"({changed['model_mode']})."
            )
        return command_usage("/effort [<level>]")

    return (
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


__all__ = ["build_llm_commands"]
