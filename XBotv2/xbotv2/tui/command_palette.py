"""Searchable palette for client and server slash commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from xbotv2.tui.command import CommandSpec, search_commands


if TYPE_CHECKING:  # pragma: no cover
    from xbotv2.tui.textual_client import XBotTextualApp


class CommandPalette(ModalScreen[None]):
    """Fuzzy-search modal for slash commands."""

    DEFAULT_CSS = """
    CommandPalette {
        align: center middle;
    }
    CommandPalette > Container {
        width: 60%;
        max-width: 80;
        height: auto;
        max-height: 16;
        background: #171a21;
        border: thick #7aa2f7;
        padding: 1 2;
    }
    CommandPalette Input {
        background: #0f1115;
        color: #d6dae2;
        border: tall #2d3440;
    }
    CommandPalette Input:focus {
        border: tall #7aa2f7;
    }
    CommandPalette .palette-row {
        height: 1;
        width: 100%;
        color: #8b95a7;
    }
    CommandPalette .palette-row.active {
        background: #2d3440;
        color: #d6dae2;
        text-style: bold;
    }
    CommandPalette .palette-empty {
        height: 1;
        color: #8b95a7;
        text-style: italic;
    }
    CommandPalette #palette-list {
        height: auto;
        max-height: 10;
        scrollbar-size: 1 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Cancel", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._matches: list[CommandSpec] = []
        self._selected: int = 0

    def compose(self) -> ComposeResult:
        with Container():
            yield Input(
                placeholder="Type to search slash commands…",
                id="palette-input",
            )
            with VerticalScroll(id="palette-list"):
                yield Static("loading…", id="palette-empty", classes="palette-empty")

    def on_mount(self) -> None:
        self.query_one("#palette-input", Input).focus()
        self._refresh_results()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh_results(query=event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        del event
        self._invoke_selected()

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "down":
            event.stop()
            event.prevent_default()
            self._move(1)
        elif event.key == "up":
            event.stop()
            event.prevent_default()
            self._move(-1)

    def _move(self, delta: int) -> None:
        if not self._matches:
            return
        self._selected = (self._selected + delta) % len(self._matches)
        self._reapply_active()

    def _refresh_results(self, *, query: str = "") -> None:
        self._matches = search_commands(query)
        if self._selected >= len(self._matches):
            self._selected = 0

        container = self.query_one("#palette-list", VerticalScroll)
        for child in list(container.children):
            child.remove()
        if not self._matches:
            container.mount(
                Static("(no matching commands)", classes="palette-empty")
            )
            return

        for index, spec in enumerate(self._matches):
            label = spec.short_label or spec.display_label
            classes = "palette-row active" if index == self._selected else "palette-row"
            container.mount(Static(f"  {label}", classes=classes))

    def _reapply_active(self) -> None:
        container = self.query_one("#palette-list", VerticalScroll)
        for index, child in enumerate(container.children):
            classes = "palette-row active" if index == self._selected else "palette-row"
            child.set_classes(classes)
        if container.children:
            container.children[self._selected].scroll_visible(
                animate=False,
                immediate=True,
            )

    def _invoke_selected(self) -> None:
        if not self._matches:
            self.dismiss()
            return
        spec = self._matches[self._selected]
        self.dismiss()
        app: "XBotTextualApp" = self.app  # type: ignore[assignment]
        app.call_after_refresh(app._handle_slash_command, spec)

    def action_dismiss(self) -> None:
        self.dismiss()
