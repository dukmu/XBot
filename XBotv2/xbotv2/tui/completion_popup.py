"""Slash-command completion popup for the composer.

Mounted directly above the composer in the XBotTextualApp layout. The
popup shows a small list of candidate slash commands whenever the
composer text starts with ``/``; the user can accept the highlighted
suggestion with ``Tab`` and dismiss the popup with ``Escape``.

The popup is intentionally minimal:

- A single ``Static`` widget (not a modal) that renders a ``rich.text.Text``
  block. One row is highlighted with reverse-bold; the rest are dim.
- The popup never receives keyboard focus; the composer keeps focus
  and ``Tab`` is intercepted at the ``ComposerTextArea`` level. This
  preserves the doc §3 invariant: "整个 TUI 同一时刻只有一个键盘焦点区".
- When the composer text does not start with ``/``, the popup renders
  blank but stays mounted (avoids re-mount cost on every keystroke).
"""

from __future__ import annotations

from rich.text import Text
from textual.widget import Widget
from textual.widgets import Static

from xbotv2.tui.command import CommandSpec, search_commands


class CompletionPopup(Static):
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
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__("", markup=False, *args, **kwargs)
        self._matches: list[CommandSpec] = []
        self._selected: int = 0
        self._visible: bool = False

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
            self.update("")
            return

        matches = search_commands(stripped)
        if not matches:
            self._matches = []
            self._selected = 0
            self._visible = False
            self.set_class(False, "active")
            self.update("")
            return

        # Clamp the selection to the new match set.
        self._matches = matches
        if self._selected >= len(matches):
            self._selected = len(matches) - 1
        self._visible = True
        self.set_class(True, "active")
        self.update(self._render())

    def _render(self) -> Text:
        if not self._matches:
            return Text()
        text = Text()
        text.append("▸ ", style="bold #7aa2f7")
        for index, spec in enumerate(self._matches):
            if index:
                text.append("   ")
            label = spec.short_label or spec.display_label
            if index == self._selected:
                text.append(f" {label} ", style="reverse bold")
            else:
                text.append(label, style="dim")
        return text

    def move_selection(self, delta: int) -> None:
        """Move the selection by ``delta`` steps, wrapping around."""

        if not self._matches:
            return
        self._selected = (self._selected + delta) % len(self._matches)
        self.update(self._render())

    def current_match(self) -> CommandSpec | None:
        if not self._matches:
            return None
        return self._matches[self._selected]
