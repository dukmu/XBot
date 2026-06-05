"""Slash command registry for the TUI composer.

v1 ships four commands per the design document §9.2:

- ``/exit`` (alias ``/quit``): quit the TUI.
- ``/clear``: clear the event stream (session/thread preserved).
- ``/help``: append help text to the event stream.
- ``/status``: append a current-state summary to the event stream.

The registry returns a ``CommandSpec`` describing what the caller should do;
the actual side effects (exit, clear, append notice) live on the app and are
invoked by the composer. The registry only classifies input.

Design constraints honored:

- Unknown ``/foo`` is reported as a "not implemented" notice, not sent to
  the server as a normal message (per §9.2 of the design doc).
- Slash detection is conservative: a leading ``/`` followed by word chars
  counts; whitespace terminates the command name.
- Aliases resolve before unknown-command reporting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CommandName = Literal["exit", "clear", "help", "status", "unknown"]


@dataclass(frozen=True)
class CommandSpec:
    """Parsed result of a slash command line."""

    name: CommandName
    args: str
    raw: str
    display_label: str


# Aliases are normalised to the canonical command name. Keys are the
# exact text the user must type (with leading slash, lowercase).
_ALIASES: dict[str, str] = {
    "/exit": "exit",
    "/quit": "exit",
    "/q": "exit",
    "/clear": "clear",
    "/help": "help",
    "/status": "status",
}


# Canonical command metadata: the human label rendered in the help/clear
# confirmation row, and the docstring shown by ``/help``.
_COMMANDS: dict[str, CommandSpec] = {
    "exit": CommandSpec(
        name="exit",
        args="",
        raw="/exit",
        display_label="/exit (alias: /quit, /q) — quit XBotv2 TUI",
    ),
    "clear": CommandSpec(
        name="clear",
        args="",
        raw="/clear",
        display_label="/clear — clear the event stream (session/thread preserved)",
    ),
    "help": CommandSpec(
        name="help",
        args="",
        raw="/help",
        display_label="/help — list available slash commands",
    ),
    "status": CommandSpec(
        name="status",
        args="",
        raw="/status",
        display_label="/status — append a current-state summary to the stream",
    ),
}


def parse_slash_command(text: str) -> CommandSpec | None:
    """Return a ``CommandSpec`` if ``text`` is a recognised slash command.

    Returns ``None`` for normal text (no leading ``/``) and a spec with
    ``name="unknown"`` for unrecognised slash commands. The spec preserves
    the original text so the caller can echo it back as a notice.
    """

    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    head, _, _tail = stripped.partition(" ")
    canonical = _ALIASES.get(head.lower())
    if canonical is None:
        return CommandSpec(
            name="unknown",
            args=stripped[len(head):],
            raw=stripped,
            display_label=f"{stripped} — not implemented in this build",
        )
    return _COMMANDS[canonical]


def known_command_labels() -> tuple[str, ...]:
    """Stable order of help-text lines, used by the ``/help`` handler."""

    return tuple(spec.display_label for spec in _COMMANDS.values())


def is_slash_command(text: str) -> bool:
    """Quick classification without allocating a spec."""

    return text.strip().startswith("/")
