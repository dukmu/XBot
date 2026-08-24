"""Typed slash-command operations owned by the commands capability."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from XBotv2.core.errors import OperationError
from XBotv2.core.operations import EmptyRequest, Operation

CommandHandler = Callable[[str], Awaitable["CommandResult"]]
_COMMAND_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class CommandResult:
    message: str
    status: Literal["ok", "error"] = "ok"


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    description: str
    kind: Literal["server", "prompt"] = "server"
    handler: CommandHandler | None = None
    usage: str = ""
    examples: tuple[str, ...] = ()
    parameters: dict[str, str] = field(default_factory=dict)
    exclusive: bool = True

    def __post_init__(self) -> None:
        if not _COMMAND_NAME.fullmatch(self.name):
            raise ValueError(
                "command name must use lowercase letters, digits, hyphens, or underscores"
            )
        if self.kind not in {"server", "prompt"}:
            raise ValueError("command kind must be server or prompt")
        if self.kind == "server" and self.handler is None:
            raise ValueError("server command requires a handler")
        if self.kind == "prompt" and self.handler is not None:
            raise ValueError("prompt command must not define a handler")


def split_command_args(raw_args: str) -> list[str]:
    """Split one command's raw argument string with shell quoting rules."""
    try:
        return shlex.split(raw_args)
    except ValueError as error:
        raise ValueError(f"Invalid command syntax: {error}") from error


def command_error(message: str) -> CommandResult:
    return CommandResult(message, status="error")


def command_usage(usage: str) -> CommandResult:
    return command_error(f"Usage: {usage}")


def guard_command(handler: CommandHandler) -> CommandHandler:
    """Convert command boundary failures into error results."""

    async def wrapped(raw_args: str) -> CommandResult:
        try:
            return await handler(raw_args)
        except (OperationError, ValueError) as error:
            return command_error(str(error))

    return wrapped


async def run_command_operation(coroutine: Any, render: Any) -> CommandResult:
    """Run one typed operation and render failures as command results."""
    try:
        data = await coroutine
    except (OperationError, ValueError) as error:
        return CommandResult(str(error), status="error")
    result = render(data)
    if isinstance(result, CommandResult):
        return result
    return CommandResult(result)


@dataclass(frozen=True, slots=True)
class CommandDescription:
    name: str
    slash: str
    kind: Literal["client", "server", "prompt"]
    description: str
    usage: str
    examples: tuple[str, ...]
    parameters: dict[str, str]
    exclusive: bool


@dataclass(frozen=True, slots=True)
class CommandCatalog:
    commands: tuple[CommandDescription, ...]


@dataclass(frozen=True, slots=True)
class ExecuteCommand:
    command: str
    kind: Literal["server", "prompt"]
    raw_args: str
    exclusive: bool = True


@dataclass(frozen=True, slots=True)
class CommandExecution:
    command: str
    status: Literal["ok", "error"]
    message: str


LIST_COMMANDS = Operation("commands/list", EmptyRequest, CommandCatalog)
EXECUTE_COMMAND = Operation(
    "commands/execute",
    ExecuteCommand,
    CommandExecution,
    exclusive=lambda request: request.exclusive,
)


__all__ = [
    "Command",
    "CommandCatalog",
    "CommandDescription",
    "CommandExecution",
    "CommandHandler",
    "CommandResult",
    "EXECUTE_COMMAND",
    "ExecuteCommand",
    "LIST_COMMANDS",
    "command_error",
    "command_usage",
    "guard_command",
    "run_command_operation",
    "split_command_args",
]
