"""Human-facing server and prompt command contracts."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from XBotv2.core.errors import OperationError

CommandHandler = Callable[[str], Awaitable["CommandResult"]]
_COMMAND_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class CommandResult:
    message: str
    status: Literal["ok", "error"] = "ok"


def split_command_args(raw_args: str) -> list[str]:
    """Split one command's raw argument string with shell quoting rules."""
    try:
        return shlex.split(raw_args)
    except ValueError as error:
        raise ValueError(f"Invalid command syntax: {error}") from error


def command_error(message: str) -> CommandResult:
    """Build a failing command result without raising through the wire."""
    return CommandResult(message, status="error")


def command_usage(usage: str) -> CommandResult:
    return command_error(f"Usage: {usage}")


def guard_command(
    handler: CommandHandler,
) -> CommandHandler:
    """Convert handler failures into command error results.

    Wraps one command handler so argument-parsing errors and rejected
    application use cases surface as a normal ``CommandResult`` with
    ``status="error"`` instead of raising through the wire endpoint.
    """

    async def wrapped(raw_args: str) -> CommandResult:
        try:
            return await handler(raw_args)
        except (OperationError, ValueError) as error:
            return command_error(str(error))

    return wrapped


async def run_command_operation(
    coroutine: Any,
    render: Any,
) -> CommandResult:
    """Run one use case and render it as a command result.

    Command handlers use this instead of awaiting a use case directly:
    ``OperationError`` / ``ValueError`` become a ``CommandResult`` with
    ``status="error"`` (never a raised wire error), and ``render(data)`` may
    return a ``CommandResult`` or a message string.
    """
    try:
        data = await coroutine
    except OperationError as error:
        return CommandResult(str(error), status="error")
    except ValueError as error:
        return CommandResult(str(error), status="error")
    result = render(data)
    if isinstance(result, CommandResult):
        return result
    return CommandResult(result)


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    description: str
    kind: Literal["server", "prompt"] = "server"
    handler: CommandHandler | None = None
    usage: str = ""
    examples: tuple[str, ...] = ()
    parameters: dict[str, str] = field(default_factory=dict)

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


__all__ = [
    "Command",
    "CommandHandler",
    "CommandResult",
    "command_error",
    "command_usage",
    "guard_command",
    "run_command_operation",
    "split_command_args",
]
