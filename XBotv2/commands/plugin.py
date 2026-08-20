"""Commands component: the user slash-command registry as an XCore service.

Capability plugins register human-facing ``Command`` values through
``ctx.commands``; registration is a fiber effect, so it is undone when the
registering plugin unloads.
"""

from __future__ import annotations

from typing import Any

from XBotv2.core.commands import Command
from XBotv2.commands.contracts import (
    CommandCatalog,
    CommandDescription,
    CommandExecution,
    EXECUTE_COMMAND,
    ExecuteCommand,
    LIST_COMMANDS,
)
from XBotv2.core.operations import EmptyRequest
from xcore import bound_effect


class CommandsService:
    """Plugin-facing command registry with fiber-scoped auto-unregister."""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def register(self, command: Command) -> str:
        if command.name in self._commands:
            raise ValueError(f"Command {command.name!r} is already registered")
        self._commands[command.name] = command
        bound_effect(lambda: self._commands.pop(command.name, None))
        return command.name

    def unregister(self, name: str) -> bool:
        return self._commands.pop(name, None) is not None

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def all(self) -> tuple[Command, ...]:
        return tuple(self._commands.values())

    def __len__(self) -> int:
        return len(self._commands)


class CommandsComponent:
    """Register the command registry as ``ctx.commands``."""

    name = "xbot.commands"

    def apply(self, ctx: Any, config: Any = None) -> None:
        service = CommandsService()
        ctx.set("commands", service)

        def list_commands(_request: EmptyRequest) -> CommandCatalog:
            return CommandCatalog(commands=tuple(
                CommandDescription(
                    name=command.name,
                    slash=f"/{command.name}",
                    kind=command.kind,
                    description=command.description,
                    usage=command.usage or f"/{command.name}",
                    examples=command.examples,
                    parameters=command.parameters,
                )
                for command in service.all()
            ))

        async def execute_command(
            request: ExecuteCommand,
        ) -> CommandExecution:
            name = request.command.lower().strip().removeprefix("/")
            if request.kind == "prompt":
                return CommandExecution(
                    command=name,
                    status="error",
                    message=(
                        "Prompt expansions must be submitted through the "
                        "message endpoint."
                    ),
                )
            command = service.get(name)
            if command is None or command.kind != "server":
                return CommandExecution(
                    command=name,
                    status="error",
                    message=f"Unknown server command: /{name}",
                )
            assert command.handler is not None
            result = await command.handler(request.raw_args)
            return CommandExecution(
                command=name,
                status=result.status,
                message=result.message,
            )

        ctx.on(LIST_COMMANDS.name, list_commands)
        ctx.on(EXECUTE_COMMAND.name, execute_command)


plugin = CommandsComponent()
