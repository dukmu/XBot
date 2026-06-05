"""Slash command registry for the TUI composer.

v1 ships four commands per the design document §9.2:

- ``/exit`` (alias ``/quit``, ``/q``): quit the TUI.
- ``/clear``: clear the event stream (session/thread preserved).
- ``/help``: append help text to the event stream.
- ``/status``: append a current-state summary to the event stream.

The registry returns a ``CommandSpec`` describing what the caller should do;
the actual side effects (exit, clear, append notice) live on the app and are
invoked by the composer. The registry only classifies input.

The registry also drives two v1.1 surfaces:

- **Slash completion** — when the composer text starts with ``/``,
  :func:`search_commands` returns the commands whose name matches
  the typed prefix. The completion popup uses this to render the
  candidate list and Tab accepts the highlighted entry.
- **Command palette** (Ctrl+P) — :func:`search_commands` also accepts
  arbitrary fuzzy queries and returns a ranked list, which the
  palette dialog renders.

Design constraints honored:

- Unknown ``/foo`` is reported as a "not implemented" notice, not sent to
  the server as a normal message (per §9.2 of the design doc).
- Slash detection is conservative: a leading ``/`` followed by word chars
  counts; whitespace terminates the command name.
- Aliases resolve before unknown-command reporting.
- Completion is case-insensitive on the leading prefix only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

CommandName = Literal["exit", "clear", "help", "status", "unknown"]


@dataclass(frozen=True)
class CommandSpec:
    """Parsed result of a slash command line."""

    name: CommandName
    args: str
    raw: str
    display_label: str
    # Optional short description used by the completion popup and the
    # command palette. Defaults to the same text as display_label for
    # backward compatibility.
    short_label: str = field(default="")

    def __post_init__(self) -> None:
        # object.__setattr__ is needed because the dataclass is frozen.
        if not self.short_label:
            object.__setattr__(self, "short_label", self.display_label)


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
        short_label="/exit  — quit XBotv2 TUI",
    ),
    "clear": CommandSpec(
        name="clear",
        args="",
        raw="/clear",
        display_label="/clear — clear the event stream (session/thread preserved)",
        short_label="/clear — clear the event stream",
    ),
    "help": CommandSpec(
        name="help",
        args="",
        raw="/help",
        display_label="/help — list available slash commands",
        short_label="/help — list slash commands",
    ),
    "status": CommandSpec(
        name="status",
        args="",
        raw="/status",
        display_label="/status — append a current-state summary to the stream",
        short_label="/status — append current state",
    ),
}


# Stable search order: keep the help/clear pair first so the most
# frequently used commands surface at the top of the completion popup
# and the palette.
_SEARCH_ORDER: tuple[str, ...] = ("help", "clear", "status", "exit")


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

    return tuple(_COMMANDS[order].display_label for order in _SEARCH_ORDER)


def is_slash_command(text: str) -> bool:
    """Quick classification without allocating a spec."""

    return text.strip().startswith("/")


# ----------------------------------------------------------------------
# Search / completion (v1.1)
# ----------------------------------------------------------------------


def _normalise_query(query: str) -> str:
    """Strip and lower-case a query for matching."""

    return query.strip().lower()


def search_commands(query: str) -> list[CommandSpec]:
    """Return commands matching ``query``, in stable display order.

    Two matching modes:

    - If ``query`` starts with ``/`` (slash completion): match on the
      command's canonical name with a case-insensitive prefix test.
    - Otherwise (palette / fuzzy): match if every whitespace-separated
      word in ``query`` is a substring of the command's
      ``short_label``. Commands that match on a leading ``/name`` get
      ranked before fuzzy matches.

    The returned list is deduplicated and capped at every registered
    command so the caller can safely show one entry per command.
    """

    normalised = _normalise_query(query)
    if not normalised:
        return [_COMMANDS[name] for name in _SEARCH_ORDER]
    if normalised.startswith("/"):
        prefix = normalised[1:]
        scored: list[tuple[int, CommandSpec]] = []
        for name in _SEARCH_ORDER:
            spec = _COMMANDS[name]
            short = spec.name
            if short.startswith(prefix) or name.startswith(prefix):
                # Exact prefix matches first; otherwise fall back to
                # substring matches inside the short label.
                score = 0 if short.startswith(prefix) else 1
                scored.append((score, spec))
                continue
            if prefix and prefix in spec.short_label.lower():
                scored.append((2, spec))
        scored.sort(key=lambda item: (item[0], _SEARCH_ORDER.index(item[1].name)))
        return [spec for _score, spec in scored]

    # Fuzzy palette mode: every whitespace-separated word in the query
    # must appear (substring, case-insensitive) in the short label.
    words = [word for word in normalised.split() if word]
    scored: list[tuple[int, CommandSpec]] = []
    for name in _SEARCH_ORDER:
        spec = _COMMANDS[name]
        haystack = spec.short_label.lower()
        if all(word in haystack for word in words):
            # Higher score = weaker match. All-word matches with shorter
            # labels rank first; we approximate by counting how many
            # extra characters the label has beyond the longest word.
            longest = max(len(word) for word in words)
            scored.append((len(haystack) - longest, spec))
    scored.sort(key=lambda item: (item[0], _SEARCH_ORDER.index(item[1].name)))
    return [spec for _score, spec in scored]


def complete_command(prefix: str) -> CommandSpec | None:
    """Return the single best completion for a slash prefix.

    ``prefix`` is the current composer text (already including the
    leading ``/``). The first match from :func:`search_commands` is
    returned, or ``None`` if nothing matches. Used by the Tab key in
    the composer to fill in the longest unambiguous completion.
    """

    if not prefix.startswith("/"):
        return None
    matches = search_commands(prefix)
    return matches[0] if matches else None
