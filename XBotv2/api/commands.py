"""Human-facing server and prompt command contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

CommandHandler = Callable[[Any, str], Awaitable["CommandResult"]]
_COMMAND_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class CommandResult:
    message: str
    status: Literal["ok", "error"] = "ok"
    data: Any = None
    history: list[dict[str, Any]] | None = None


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


__all__ = ["Command", "CommandHandler", "CommandResult"]
