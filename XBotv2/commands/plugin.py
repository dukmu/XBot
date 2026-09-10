"""Commands component: the user slash-command registry as an XCore service.

Capability plugins register human-facing ``Command`` values through
``ctx.commands``; registration is a fiber effect, so it is undone when the
registering plugin unloads.
"""

from __future__ import annotations

from functools import partial
from pydantic import JsonValue

from xcore import Context, bound_effect
from XBotv2.commands.contracts import (
    Command,
    CommandCatalog,
    CommandDescription,
    CommandExecution,
    CommandsPort,
    EXECUTE_COMMAND,
    ExecuteCommand,
    LIST_COMMANDS,
)
from XBotv2.commands.protocol import build_commands_router
from XBotv2.server import contribute_router


async def mount_http(ctx: Context) -> None:
    await contribute_router(
        ctx,
        owner="xbot.commands.http",
        router=build_commands_router(sessions=ctx.sessions),
    )
from XBotv2.core.operations import EmptyRequest


class CommandsService(CommandsPort):
    """Plugin-facing command registry with fiber-scoped auto-unregister."""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def register(self, command: Command) -> str:
        if command.name in self._commands:
            raise ValueError(f"Command {command.name!r} is already registered")
        self._commands[command.name] = command
        bound_effect(partial(self.unregister, command.name))
        return command.name

    def unregister(self, name: str) -> bool:
        return self._commands.pop(name, None) is not None

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def all(self) -> tuple[Command, ...]:
        return tuple(self._commands.values())

    def __len__(self) -> int:
        return len(self._commands)


class CommandOperations:
    def __init__(self, commands: CommandsService) -> None:
        self._commands = commands

    def list_commands(self, _request: EmptyRequest) -> CommandCatalog:
        return CommandCatalog(commands=tuple(
            CommandDescription(
                name=command.name,
                slash=f"/{command.name}",
                kind=command.kind,
                description=command.description,
                usage=command.usage or f"/{command.name}",
                examples=command.examples,
                parameters=command.parameters,
                exclusive=command.exclusive,
            )
            for command in self._commands.all()
        ))

    async def execute_command(self, request: ExecuteCommand) -> CommandExecution:
        name = request.command.lower().strip().removeprefix("/")
        if request.kind == "prompt":
            return CommandExecution(
                command=name,
                status="error",
                message="Prompt expansions must be submitted through the message endpoint.",
            )
        command = self._commands.get(name)
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
            effects=result.effects if result.status == "ok" else (),
        )


class CommandsComponent:
    """Register the command registry as ``ctx.commands``."""

    name = "xbot.commands"

    def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        service = CommandsService()
        ctx.set("commands", service)
        operations = CommandOperations(service)
        ctx.on(LIST_COMMANDS.name, operations.list_commands)
        ctx.on(EXECUTE_COMMAND.name, operations.execute_command)
        ctx.inject(["server", "sessions"], mount_http)


plugin = CommandsComponent()
