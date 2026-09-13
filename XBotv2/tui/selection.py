"""Codex-like single-choice list for commands that would otherwise need
typing an id (threads, sessions, providers)."""

from __future__ import annotations

from collections.abc import Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

class _SelectionRow(Static):
    def __init__(self, index: int, screen: "SelectionScreen") -> None:
        super().__init__("", classes="selection-row")
        self._index = index
        self._screen = screen

    def on_click(self, event) -> None:  # type: ignore[no-untyped-def]
        event.stop()
        self._screen._choose_at(self._index)


class SelectionScreen(ModalScreen[str]):
    """Blocking modal: up/down + Enter picks one option, Esc cancels.

    Options are ``(value, display_label)`` pairs; dismissing returns the
    chosen value, or ``None`` when cancelled.
    """

    DEFAULT_CSS = """
    SelectionScreen {
        align: center middle;
    }
    SelectionScreen > Container {
        width: 62%;
        max-width: 86;
        height: auto;
        max-height: 18;
        background: #171a21;
        border: thick #7aa2f7;
        padding: 1 2;
    }
    SelectionScreen #selection-title {
        height: 1;
        margin-bottom: 1;
        color: #7dcfff;
        text-style: bold;
    }
    SelectionScreen .selection-row {
        height: 1;
        width: 100%;
        color: #8b95a7;
    }
    SelectionScreen .selection-row.active {
        background: #2d3440;
        color: #d6dae2;
        text-style: bold;
    }
    SelectionScreen #selection-list {
        height: auto;
        max-height: 14;
        scrollbar-size: 1 1;
    }
    """

    BINDINGS = [Binding("escape", "dismiss", "Cancel", show=False)]

    def __init__(
        self,
        title: str,
        options: Sequence[tuple[str, str]],
    ) -> None:
        super().__init__()
        self._title = title
        self._options = list(options)
        self._selected = 0

    def compose(self) -> ComposeResult:
        with Container():
            yield Static(self._title, id="selection-title")
            with VerticalScroll(id="selection-list"):
                for index in range(len(self._options)):
                    yield _SelectionRow(index, self)

    def on_mount(self) -> None:
        self._populate()
        self.focus_next()

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "down":
            event.stop()
            event.prevent_default()
            self._move(1)
        elif event.key == "up":
            event.stop()
            event.prevent_default()
            self._move(-1)
        elif event.key == "enter":
            event.stop()
            event.prevent_default()
            self._choose_at(self._selected)

    def _populate(self) -> None:
        rows = self.query(_SelectionRow)
        for row, (_value, label) in zip(rows, self._options, strict=False):
            row.update(label)
        self._reapply_active()

    def _move(self, delta: int) -> None:
        if not self._options:
            return
        self._selected = (self._selected + delta) % len(self._options)
        self._reapply_active()

    def _reapply_active(self) -> None:
        for index, row in enumerate(self.query(_SelectionRow)):
            row.set_class(index == self._selected, "active")

    def _choose_at(self, index: int) -> None:
        if 0 <= index < len(self._options):
            self.dismiss(self._options[index][0])
        else:
            self.dismiss(None)