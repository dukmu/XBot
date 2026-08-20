"""Loader-owned soft reload operation and `/reload` command."""

from __future__ import annotations

from typing import Any

from XBotv2.commands import Command, CommandResult, command_usage, guard_command
from XBotv2.core.operations import EmptyRequest
from XBotv2.loader.contracts import (
    RELOAD_PLUGINS,
    SOFT_RELOAD,
    Reloaded,
    SoftReload,
)


class LoaderReloadComponent:
    name = "xbot.loader.reload"
    inject = ["loader", "reload_plan", "agent_runtime", "commands"]

    def apply(self, ctx: Any, config: Any = None) -> None:
        async def reload_plugins(_request: EmptyRequest) -> Reloaded:
            event = SoftReload(
                scope="system",
                config_path=ctx.reload_plan.config_path,
                variables=dict(ctx.reload_plan.variables),
            )
            await ctx.emit(SOFT_RELOAD, event)
            selected = ctx.agent_runtime.current_selection()
            return Reloaded(
                reloaded=tuple(event.reloaded),
                errors=tuple(event.errors),
                provider=selected.provider,
                model=selected.model,
                model_mode=selected.model_mode,
                context_window=selected.context_window,
            )

        async def reload_command(raw_args: str) -> CommandResult:
            if raw_args.strip():
                return command_usage("/reload")
            data = await reload_plugins(EmptyRequest())
            message = (
                f"Reloaded {', '.join(data.reloaded)}: "
                f"{data.provider} ({data.model})"
            )
            if data.errors:
                message += "; errors: " + "; ".join(data.errors)
            return CommandResult(message)

        ctx.on(RELOAD_PLUGINS.name, reload_plugins)
        ctx.commands.register(Command(
            name="reload",
            description="Re-read config overlays and re-apply plugins",
            handler=guard_command(reload_command),
            usage="/reload",
        ))


plugin = LoaderReloadComponent()
