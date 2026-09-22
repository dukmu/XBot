"""Picking a command: the palette is the selection list over a command search.

There is no separate palette model. ``CommandPalette`` is a
:class:`SelectionScreen` whose options come from ``CommandRegistry.search``, so the
palette and the slash completion stay consistent about what matches, and there is
one implementation of "navigate a filtered list".
"""

from __future__ import annotations

from typing import Sequence

from XBotv2.commands import CommandDescription
from XBotv2.tui.commands import CommandRegistry
from XBotv2.tui.view.selection import Option, SelectionScreen

PALETTE_CSS = """
CommandPalette #selection {
    width: 90%;
    max-width: 120;
    max-height: 90%;
}
"""


def command_options(specs: Sequence[CommandDescription]) -> tuple[Option, ...]:
    """One option per command, labelled the way the palette shows it."""
    return tuple(
        Option(
            value=f"/{spec.name}",
            label=f"/{spec.name}",
            detail=spec.description,
        )
        for spec in specs
    )


class CommandPalette(SelectionScreen):
    """A searchable list of the commands the client can run."""

    DEFAULT_CSS = SelectionScreen.DEFAULT_CSS + PALETTE_CSS

    def __init__(self, registry: CommandRegistry) -> None:
        self.registry = registry
        super().__init__(
            title="Commands",
            options=command_options(registry.search("")),
            search=lambda query: command_options(registry.search(query)),
            placeholder="search commands",
        )


__all__ = ["PALETTE_CSS", "CommandPalette", "command_options"]
