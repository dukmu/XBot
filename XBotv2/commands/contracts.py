"""Typed slash-command operations owned by the commands capability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from XBotv2.core.operations import EmptyRequest, Operation


@dataclass(frozen=True, slots=True)
class CommandDescription:
    name: str
    slash: str
    kind: Literal["client", "server", "prompt"]
    description: str
    usage: str
    examples: tuple[str, ...]
    parameters: dict[str, str]


@dataclass(frozen=True, slots=True)
class CommandCatalog:
    commands: tuple[CommandDescription, ...]


@dataclass(frozen=True, slots=True)
class ExecuteCommand:
    command: str
    kind: Literal["server", "prompt"]
    raw_args: str


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
    exclusive=True,
)


__all__ = [
    "CommandCatalog",
    "CommandDescription",
    "CommandExecution",
    "EXECUTE_COMMAND",
    "ExecuteCommand",
    "LIST_COMMANDS",
]
