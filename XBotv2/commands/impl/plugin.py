"""Commands component: the user slash-command registry as an XCore service.

Capability plugins register human-facing ``Command`` values through
``ctx.commands``; registration is a fiber effect, so it is undone when the
registering plugin unloads.
"""

from __future__ import annotations
import asyncio

from typing import Literal
from pydantic import JsonValue
from xcore.api.plugins import bound_effect
from xcore.api.context import Context
from XBotv2.commands.api.contracts import (
    COMMANDS,
    Command,
    CommandCatalog,
    CommandExecution,
    CommandsPort,
    EXECUTE_COMMAND,
    ExecuteCommand,
    LIST_COMMANDS,
    describe_command,
)
from XBotv2.commands.impl.protocol import build_commands_router
from XBotv2.server.api.contracts import contribute_router


async def mount_http(ctx: Context) -> None:
    await contribute_router(
        ctx,
        owner="xbot.commands.http",
        router=build_commands_router(sessions=ctx.sessions),
    )
from XBotv2.core.api.operations import EmptyRequest


class CommandsService(CommandsPort):
    """Plugin-facing command registry with fiber-scoped auto-unregister."""

    def __init__(self, *, ownership: Literal["fiber", "caller"] = "fiber", owner: Context | None = None) -> None:
        self._commands: dict[str, tuple[object, Command]] = {}
        # Composition-level default for registrations made outside a plugin
        # apply: "fiber" stays loud, "caller" states the composer owns release.
        self._ownership = ownership
        self._owner = owner
        self._closing = False
        self._calls = set()

    def register(
        self,
        command: Command,
        *,
        owner: Context | None = None,
        cleanup: Literal["fiber", "caller"] | None = None,
    ) -> str:
        if self._closing or (self._owner is not None and not self._owner.is_active):
            raise RuntimeError("command registry is closing")
        if command.name in self._commands:
            raise ValueError(f"Command {command.name!r} is already registered")
        token = object()
        self._commands[command.name] = (token, command)

        def remove() -> None:
            entry = self._commands.get(command.name)
            if entry is not None and entry[0] is token:
                self.unregister(command.name)

        try:
            if owner is not None:
                owner.effect(lambda: remove)
            elif bound_effect(remove) is False and (cleanup or self._ownership) != "caller":
                raise RuntimeError(
                    f"Command {command.name!r} was registered outside a plugin "
                    "apply(); provide an owner or pass cleanup='caller' when the caller owns release"
                )
        except BaseException:
            remove()
            raise
        return command.name

    async def invoke(self, command, args):
        if self._closing or (self._owner is not None and not self._owner.is_active):
            raise RuntimeError("command registry is closing")
        task = asyncio.current_task()
        self._calls.add(task)
        try:
            return await command.handler(args)
        finally:
            self._calls.discard(task)

    async def close(self):
        self._closing = True
        calls = [task for task in self._calls if task is not asyncio.current_task()]
        for task in calls:
            task.cancel()
        if calls:
            await asyncio.gather(*calls, return_exceptions=True)

    def unregister(self, name: str) -> bool:
        return self._commands.pop(name, None) is not None

    def get(self, name: str) -> Command | None:
        entry = self._commands.get(name)
        return entry[1] if entry is not None else None

    def all(self) -> tuple[Command, ...]:
        return tuple(command for _, command in self._commands.values())

    def __len__(self) -> int:
        return len(self._commands)


class CommandOperations:
    def __init__(self, commands: CommandsService) -> None:
        self._commands = commands

    def list_commands(self, _request: EmptyRequest) -> CommandCatalog:
        return CommandCatalog(commands=tuple(
            describe_command(command)
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
        result = await self._commands.invoke(command, request.raw_args)
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
        service = CommandsService(owner=ctx)
        ctx.set(COMMANDS, service)
        ctx.pre_stop(service.close)
        operations = CommandOperations(service)
        ctx.on(LIST_COMMANDS.name, operations.list_commands)
        ctx.on(EXECUTE_COMMAND.name, operations.execute_command)


def mount_thread(ctx, config):
    CommandsComponent().apply(ctx, config)

mount_thread.inject = ()
mount_thread.provide = (COMMANDS,)


def mount_process(ctx, config):
    ctx.inject(["server", "sessions"], mount_http)
    # Client roots own their command registry at process scope.
    CommandsComponent().apply(ctx, config)

mount_process.inject = ()
mount_process.provide = (COMMANDS,)
