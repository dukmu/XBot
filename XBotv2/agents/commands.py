"""Human commands owned by the agents plugin (``/agent``).

The handler uses the injected ``ctx.services.agents`` service only — the
agents plugin stays inside its capability boundary and never imports other
plugin implementations.
"""

from __future__ import annotations

from typing import Any

from XBotv2.core.commands import (
    Command,
    CommandResult,
    command_usage,
    guard_command,
    split_command_args,
)


async def agent_command(ctx: Any, raw_args: str) -> CommandResult:
    parts = split_command_args(raw_args)
    action = parts[0].lower() if parts else "status"
    if action == "reload" and len(parts) == 1:
        if ctx.turn_lock.locked():
            return _error("Cannot reload Agents while a turn is active.")
        try:
            async with ctx.turn_lock:
                data = await ctx.services.agents.reload_active()
        except (ValueError, RuntimeError) as error:
            return _error(str(error))
        ctx.provider_name = ctx.engine.settings.provider
        return CommandResult(
            f"Reloaded {len(data['agents'])} Agent definitions.",
            data={
                "active": data["active"],
                "agents": [
                    {
                        "name": definition.name,
                        "description": definition.description,
                        "mode": definition.mode,
                        "provider": definition.provider or "",
                        "model": definition.model or "",
                        "context_window": definition.context_window or 0,
                    }
                    for definition in data["agents"]
                    if not definition.hidden
                ],
            },
        )
    if action in {"status", "list"} and len(parts) <= 1:
        return _agent_list(ctx, action)
    target = parts[1] if action == "use" and len(parts) == 2 else None
    if len(parts) == 1 and action not in {"status", "list", "use"}:
        target = parts[0]
    if target is None:
        return command_usage("/agent [status|list|reload|use <name>|<name>]")
    if ctx.turn_lock.locked():
        return _error("Cannot switch Agent while a turn is active.")
    try:
        async with ctx.turn_lock:
            data = await ctx.services.agents.select(target)
        ctx.provider_name = data["provider"]
    except (ValueError, RuntimeError) as error:
        return _error(str(error))
    return CommandResult(
        f"Active Agent: {data['agent_name']}.",
        data=data,
    )


AGENTS_COMMANDS: tuple[Command, ...] = (
    Command(
        name="agent",
        description="List or switch the active primary Agent",
        handler=guard_command(agent_command),
        usage="/agent [status|list|reload|use <name>|<name>]",
    ),
)


def _agent_list(ctx: Any, action: str) -> CommandResult:
    definitions = [
        definition
        for definition in ctx.services.agents.definitions()
        if not definition.hidden
    ]
    data = {
        "active": ctx.engine.settings.agent_name,
        "agents": [
            {
                "name": definition.name,
                "description": definition.description,
                "mode": definition.mode,
                "provider": definition.provider or "",
                "model": definition.model or "",
                "context_window": definition.context_window or 0,
            }
            for definition in definitions
        ],
    }
    lines = [f"Active Agent: {data['active']}"]
    if action == "list":
        lines.extend(
            f"{item['name']}  {item['mode']}  {item['description']}"
            for item in data["agents"]
        )
    return CommandResult("\n".join(lines), data=data)


def _error(message: str) -> CommandResult:
    return CommandResult(message, status="error", data={"code": "command_failed"})


__all__ = ["AGENTS_COMMANDS", "agent_command"]
