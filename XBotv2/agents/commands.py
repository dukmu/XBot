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
    handler = AgentCommandHandler(runtime, catalog)
    return (
        Command(
            name="agent",
            description="List or switch the active primary Agent",
            handler=guard_command(handler.run),
            usage="/agent [status|list|use <name>|<name>]",
        ),
    )


class AgentCommandHandler:
    """Handle the human-facing ``/agent`` command."""

    def __init__(
        self,
        runtime: AgentRuntimePort,
        catalog: AgentCatalogPort,
    ) -> None:
        self._runtime = runtime
        self._catalog = catalog

    async def run(self, raw_args: str) -> CommandResult:
        parts = split_command_args(raw_args)
        action = parts[0].lower() if parts else "status"
        if action in {"status", "list"} and len(parts) <= 1:
            selected = self._runtime.current_selection()
            lines = [f"Active Agent: {selected.active}"]
            if action == "list":
                lines.extend(
                    f"{definition.name}  {definition.mode}  {definition.description}"
                    for definition in self._catalog.definitions()
                    if not definition.hidden
                )
            return CommandResult("\n".join(lines))
        target = parts[1] if action == "use" and len(parts) == 2 else None
        if len(parts) == 1 and action not in {"status", "list", "use"}:
            target = parts[0]
        if target is None:
            return command_usage("/agent [status|list|use <name>|<name>]")
        data = await self._runtime.select(target)
        return CommandResult(
            f"Active Agent: {data['agent_name']}.",
            effects=("thread", "agents", "commands"),
        )


__all__ = ["build_agent_commands"]
