"""Human `/agent` command declarations and typed binding factory."""

from __future__ import annotations

from XBotv2.agents.services import AgentCatalogPort, AgentRuntimePort
from XBotv2.commands import (
    Command,
    CommandResult,
    command_usage,
    guard_command,
    split_command_args,
)


def build_agent_commands(
    runtime: AgentRuntimePort,
    catalog: AgentCatalogPort,
) -> tuple[Command, ...]:
    async def agent_command(raw_args: str) -> CommandResult:
        parts = split_command_args(raw_args)
        action = parts[0].lower() if parts else "status"
        if action == "reload" and len(parts) == 1:
            data = await runtime.reload_active()
            return CommandResult(
                f"Reloaded {len(data['agents'])} Agent definitions."
            )
        if action in {"status", "list"} and len(parts) <= 1:
            selected = runtime.current_selection()
            lines = [f"Active Agent: {selected.active}"]
            if action == "list":
                lines.extend(
                    f"{definition.name}  {definition.mode}  {definition.description}"
                    for definition in catalog.definitions()
                    if not definition.hidden
                )
            return CommandResult("\n".join(lines))
        target = parts[1] if action == "use" and len(parts) == 2 else None
        if len(parts) == 1 and action not in {"status", "list", "use"}:
            target = parts[0]
        if target is None:
            return command_usage("/agent [status|list|reload|use <name>|<name>]")
        data = await runtime.select(target)
        return CommandResult(f"Active Agent: {data['agent_name']}.")

    return (
        Command(
            name="agent",
            description="List or switch the active primary Agent",
            handler=guard_command(agent_command),
            usage="/agent [status|list|reload|use <name>|<name>]",
        ),
    )


__all__ = ["build_agent_commands"]
