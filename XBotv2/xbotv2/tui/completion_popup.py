"""Slash-command completion popup for the composer.

Mounted directly above the composer in the XBotTextualApp layout. The
popup shows a small list of candidate slash commands whenever the
composer text starts with ``/``; the user can accept the highlighted
suggestion with ``Tab`` and dismiss the popup with ``Escape``.

The popup is intentionally minimal:

- A ``Container`` (Vertical) holding one ``Static`` per candidate row.
  This avoids ``rich.text.Text`` and its ``get_height`` quirks in
  Textual's layout pipeline — each row is a plain ``Static`` with
  a class for the highlighted state.
- The popup never receives keyboard focus; the composer keeps focus
  and ``Tab`` is intercepted at the ``ComposerTextArea`` level. This
  preserves the doc §3 invariant: "整个 TUI 同一时刻只有一个键盘焦点区".
- When the composer text does not start with ``/``, the popup hides
  via the ``active`` CSS class (display: none).
"""

from __future__ import annotations

from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static

from xbotv2.tui.command import CommandSpec, search_commands


_MAX_ROWS = 6


class CompletionPopup(Vertical):
    """A non-focusable completion popup for slash commands."""

    DEFAULT_CSS = """
    CompletionPopup {
        height: auto;
        max-height: 6;
        padding: 0 2;
        background: #171a21;
        color: #8b95a7;
        border: tall #2d3440;
        display: none;
    }
    CompletionPopup.active {
        display: block;
    }
    CompletionPopup Static {
        height: 1;
        width: 100%;
    }
    CompletionPopup Static.active {
        background: #2d3440;
        color: #d6dae2;
        text-style: bold;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._matches: list[CommandSpec] = []
        self._selected: int = 0
        self._visible: bool = False
        # Per-row Static widgets, kept in sync with self._matches.
        self._row_widgets: list[Static] = []

    @property
    def matches(self) -> list[CommandSpec]:
        return list(self._matches)

    @property
    def selected(self) -> int:
        return self._selected

    @property
    def visible(self) -> bool:
        return self._visible

    def update_for(self, composer_text: str) -> None:
        """Refresh the popup contents for the current composer text."""

        stripped = composer_text.lstrip()
        if not stripped.startswith("/"):
            self._matches = []
            self._selected = 0
            self._visible = False
            self.set_class(False, "active")
            self._rebuild_rows()
            return

        matches = search_commands(stripped)[:_MAX_ROWS]
        if not matches:
            self._matches = []
            self._selected = 0
            self._visible = False
            self.set_class(False, "active")
            self._rebuild_rows()
            return

        # Clamp the selection to the new match set.
        self._matches = matches
        if self._selected >= len(matches):
            self._selected = len(matches) - 1
        self._visible = True
        self.set_class(True, "active")
        self._rebuild_rows()

    def _rebuild_rows(self) -> None:
        """Re-mount the row widgets to match self._matches."""

        # Drop existing rows.
        for widget in list(self._row_widgets):
            widget.remove()
        self._row_widgets = []

        if not self._matches:
            return

        for index, spec in enumerate(self._matches):
            label = spec.short_label or spec.display_label
            classes = "active" if index == self._selected else ""
            row = Static(f"  {label}", classes=classes)
            self._row_widgets.append(row)
            self.mount(row)

    def move_selection(self, delta: int) -> None:
        """Move the selection by ``delta`` steps, wrapping around."""

        if not self._matches:
            return
        self._selected = (self._selected + delta) % len(self._matches)
        self._rebuild_rows()

    def current_match(self) -> CommandSpec | None:
        if not self._matches:
            return None
        return self._matches[self._selected]
