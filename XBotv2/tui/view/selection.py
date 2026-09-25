"""Picking one thing from a list: sessions, threads, or a command.

The list state is pure, so navigation and filtering are tested without a screen.
The screen only renders it and reports the chosen value.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Input, Static

MAX_ROWS = 12

SELECTION_CSS = """
SelectionScreen {
    align: center middle;
}
#selection {
    width: 80%;
    max-width: 100;
    height: auto;
    max-height: 80%;
    background: $panel;
    border: round $accent;
    padding: 0 1;
}
#selection-title {
    height: 1;
    text-style: bold;
}
#selection-list {
    height: auto;
    max-height: 12;
}
.selection-row {
    height: 1;
    width: 1fr;
}
.selection-row.selected {
    text-style: reverse;
}
"""


@dataclass(frozen=True)
class Option:
    """One choosable thing."""

    value: str
    label: str
    detail: str = ""


@dataclass(frozen=True)
class SelectionModel:
    """Which option is highlighted."""

    options: tuple[Option, ...] = ()
    index: int = 0

    @property
    def current(self) -> Option | None:
        if not self.options:
            return None
        return self.options[self.index]

    @property
    def rows(self) -> tuple[str, ...]:
        return tuple(
            ("▸ " if position == self.index else "  ")
            + (f"{option.label}  {option.detail}" if option.detail else option.label)
            for position, option in enumerate(self.options)
        )

    def move(self, delta: int) -> "SelectionModel":
        """Move the highlight, wrapping at either end."""
        if not self.options:
            return self
        return replace(self, index=(self.index + delta) % len(self.options))

    def with_options(self, options: Sequence[Option]) -> "SelectionModel":
        """Replace the list, highlighting its best row.

        ``options`` arrives ranked best-first -- the registry puts a name prefix
        above a mere mention -- so a new query highlights the best match, which
        is what a palette is expected to do.

        It used to hold the previous highlight while that value survived
        *anywhere* in the list and otherwise keep its row index. Typing "sta"
        therefore kept ``/help`` highlighted (its description contains an "s"),
        and when it no longer survived the same index clamped onto ``/model``,
        which Enter then chose: a command the user had never looked at.
        """
        replacements = tuple(options)
        if not replacements:
            return SelectionModel(options=(), index=0)
        return SelectionModel(options=replacements, index=0)

class SelectionScreen(ModalScreen[str | None]):
    """A modal list; dismisses with the chosen value or ``None``."""

    DEFAULT_CSS = SELECTION_CSS
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        title: str,
        options: Sequence[Option],
        *,
        search: Callable[[str], Sequence[Option]] | None = None,
        placeholder: str = "",
    ) -> None:
        super().__init__()
        self.title_text = title
        self.model = SelectionModel(options=tuple(options))
        self.search = search
        self.placeholder = placeholder
        self._rows: list[Static] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="selection"):
            yield Static(self.title_text, id="selection-title")
            if self.search is not None:
                yield Input(
                    placeholder=self.placeholder or "type to filter",
                    id="selection-input",
                )
            with VerticalScroll(id="selection-list"):
                for _ in range(min(len(self.model.options), MAX_ROWS)):
                    yield Static("", classes="selection-row")

    def on_mount(self) -> None:
        self._rows = [node for node in self.query(".selection-row")]
        self._render_options()
        if self.search is not None:
            self.query_one("#selection-input", Input).focus()

    # --- input --------------------------------------------------------

    def on_key(self, event: Key) -> None:
        if event.key == "up":
            event.stop()
            self.model = self.model.move(-1)
            self._render_options()
        elif event.key == "down":
            event.stop()
            self.model = self.model.move(1)
            self._render_options()
        elif event.key == "enter" and self.search is None:
            event.stop()
            self._choose()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self.search is None:
            return
        self.model = self.model.with_options(self.search(event.value))
        self._render_options()

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        if self.search is not None:
            self._choose()

    def action_cancel(self) -> None:
        self.dismiss(None)

    # --- rendering ----------------------------------------------------

    def _choose(self) -> None:
        current = self.model.current
        self.dismiss(current.value if current is not None else None)

    def _render_options(self) -> None:
        visible = self.model.options[:MAX_ROWS]
        for position, row in enumerate(self._rows):
            if position >= len(visible):
                row.display = False
                continue
            row.display = True
            row.set_class(position == self.model.index, "selected")
            row.update(Text(self.model.rows[position]))

    @property
    def rendered_rows(self) -> tuple[str, ...]:
        """The rows currently on screen, for tests and for inspection."""
        return tuple(
            str(row.content)
            for row in self._rows
            if row.display
        )


__all__ = ["MAX_ROWS", "SELECTION_CSS", "Option", "SelectionModel", "SelectionScreen"]
