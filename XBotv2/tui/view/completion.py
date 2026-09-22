"""Slash-command completion, as a pure state machine plus a thin popup.

"Should the popup be showing, and what is highlighted" is decided by
``completion_for``/``move``/``dismiss``/``accepted_text`` -- pure functions of the
composer text, the catalogue, and the current highlight. The widget renders that
state and owns no rules.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence

from textual.containers import Vertical
from textual.widgets import Static

from XBotv2.commands import CommandDescription
from XBotv2.tui.commands import CommandRegistry

_MAX_ROWS = 8

COMPLETION_CSS = """
CompletionPopup {
    height: auto;
    max-height: 10;
    width: 1fr;
    display: none;
}
CompletionPopup.visible {
    display: block;
}
CompletionPopup > .completion-row {
    height: 1;
    width: 1fr;
    padding: 0 1;
}
CompletionPopup > .completion-row.selected {
    text-style: reverse;
}
"""


@dataclass(frozen=True)
class CompletionState:
    """What the popup should show."""

    query: str = ""
    matches: tuple[CommandDescription, ...] = ()
    index: int = 0
    visible: bool = False

    @property
    def current(self) -> CommandDescription | None:
        if not self.visible or not self.matches:
            return None
        return self.matches[self.index]

    @property
    def rows(self) -> tuple[str, ...]:
        """The lines to render, with the highlighted one marked."""
        return tuple(
            ("▸ " if position == self.index else "  ") + f"{spec.name}  {spec.description}"
            for position, spec in enumerate(self.matches)
        )


def completion_for(
    registry: CommandRegistry,
    text: str,
    *,
    index: int = 0,
) -> CompletionState:
    """The completion state for what is currently in the composer."""
    if not _is_completing(text):
        return CompletionState(query=text)
    matches = registry.complete(text)
    if not matches:
        return CompletionState(query=text)
    return CompletionState(
        query=text,
        matches=matches,
        index=min(index, len(matches) - 1),
        visible=True,
    )


def move(state: CompletionState, delta: int) -> CompletionState:
    """Move the highlight, wrapping at either end."""
    if not state.visible or not state.matches:
        return state
    total = len(state.matches)
    return replace(state, index=(state.index + delta) % total)


def dismiss(state: CompletionState) -> CompletionState:
    """Hide the popup without forgetting what was typed."""
    return replace(state, visible=False)


def accepted_text(state: CompletionState) -> str | None:
    """The composer text after accepting the highlight, or ``None``."""
    current = state.current
    if current is None:
        return None
    return f"/{current.name}"


def _is_completing(text: str) -> bool:
    """Only while the first word of the first line is still being typed."""
    if not text.startswith("/"):
        return False
    if "\n" in text:
        return False
    return not any(character.isspace() for character in text)


class CompletionPresenter:
    """Owns the completion state and mirrors it onto the popup.

    Holding the state here (rather than in the widget) means the keyboard rules
    and the rendering can be tested separately, and the composer only needs to
    know four calls.
    """

    def __init__(self, registry: CommandRegistry) -> None:
        self.registry = registry
        self.state = CompletionState()
        self._popup: "CompletionPopup | None" = None
        # The text an accept produced. Rewriting the composer raises a change
        # event with exactly this text, and re-opening the popup for a command
        # the user just completed is noise.
        self._accepted: str | None = None

    def attach(self, popup: "CompletionPopup") -> None:
        self._popup = popup
        self._render()

    @property
    def visible(self) -> bool:
        return self.state.visible

    @property
    def current(self) -> CommandDescription | None:
        return self.state.current

    @property
    def rows(self) -> tuple[str, ...]:
        return self.state.rows

    def refresh(self, text: str) -> None:
        if text == self._accepted:
            self.state = CompletionState(query=text)
            self._render()
            return
        self._accepted = None
        self.state = completion_for(self.registry, text)
        self._render()

    def move(self, delta: int) -> None:
        self.state = move(self.state, delta)
        self._render()

    def accept(self) -> str | None:
        text = accepted_text(self.state)
        if text is not None:
            self._accepted = text
            self.state = dismiss(self.state)
            self._render()
        return text

    def dismiss(self) -> None:
        self.state = dismiss(self.state)
        self._render()

    def _render(self) -> None:
        if self._popup is not None:
            self._popup.show(self.state)


class CompletionPopup(Vertical):
    """The list of matching commands, driven by a :class:`CompletionState`."""

    DEFAULT_CSS = COMPLETION_CSS

    def __init__(self, registry: CommandRegistry, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.registry = registry
        self.state = CompletionState()
        self._rows: list[Static] = []

    @property
    def visible(self) -> bool:
        return self.state.visible

    def show(self, state: CompletionState) -> None:
        self.state = state
        wanted = state.matches[state.index : state.index + _MAX_ROWS] or state.matches[:_MAX_ROWS]
        while len(self._rows) < len(wanted):
            row = Static("", classes="completion-row")
            self._rows.append(row)
            self.mount(row)
        for position, row in enumerate(self._rows):
            if position >= len(wanted):
                row.display = False
                continue
            row.display = True
            highlighted = wanted[position] is state.current
            row.set_class(highlighted, "selected")
            row.update(
                f"{wanted[position].name}  {wanted[position].description}"
            )
        self.set_class(state.visible, "visible")


__all__ = [
    "COMPLETION_CSS",
    "CompletionPopup",
    "CompletionPresenter",
    "CompletionState",
    "accepted_text",
    "completion_for",
    "dismiss",
    "move",
]
