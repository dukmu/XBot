"""Commands component: the user slash-command registry as an XCore service.

Capability plugins register human-facing ``Command`` values through
``ctx.commands``; registration is a fiber effect, so it is undone when the
registering plugin unloads.
"""

from __future__ import annotations

from typing import Any

import logging
from typing import Any

from XBotv2.core.commands import Command
from xcore import current_fiber

logger = logging.getLogger("xbot.commands")


def _bind_cleanup(disposer: Any) -> None:
    """Register a disposer on the applying fiber (never raises)."""
    fiber = current_fiber()
    if fiber is None:
        return
    try:
        fiber.effect(lambda: disposer)
    except Exception:  # noqa: BLE001 - cleanup registration must not break setup
        logger.exception("failed to register cleanup effect")


class CommandsService:
    """Plugin-facing command registry with fiber-scoped auto-unregister."""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def register(self, command: Command) -> str:
        if command.name in self._commands:
            raise ValueError(f"Command {command.name!r} is already registered")
        self._commands[command.name] = command
        _bind_cleanup(lambda: self._commands.pop(command.name, None))
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
        ctx.set("commands", CommandsService())


plugin = CommandsComponent()
