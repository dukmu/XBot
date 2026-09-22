"""The queued-input panel.

Rows are derived from the server's pending inputs (``PendingInputData``) and the
widget reconciles them by message id, exactly as the job panel does by job id:
rebuilding the list on every tick is what made rows flicker and lose their place.

The panel shows *what is waiting*, not how much: the status line already carries
the count, and a list of "3" is of no use to a reader.
"""

from __future__ import annotations

from typing import Sequence

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from XBotv2.session.contracts import PendingInputData

QUEUE_PANEL_CSS = """
QueuePanel {
    height: auto;
    max-height: 8;
    width: 1fr;
}
QueuePanel .queue-row {
    height: auto;
    width: 1fr;
}
"""


def queue_row(
    item: PendingInputData,
    *,
    position: int = 1,
    width: int = 80,
) -> str:
    """One queued input as a single clipped line.

    The text is collapsed onto one line: a queued message may be multi-paragraph,
    and a panel that grows with its content would take the screen from the
    transcript it is waiting behind.
    """
    head = f"{position:>2}  {item.content.replace(chr(10), ' ').strip()}"
    if item.target:
        head = f"{head}  [{item.target}]"
    extras = []
    if item.image_count:
        noun = "image" if item.image_count == 1 else "images"
        extras.append(f"{item.image_count} {noun}")
    if item.artifact_count:
        noun = "file" if item.artifact_count == 1 else "files"
        extras.append(f"{item.artifact_count} {noun}")
    if extras:
        head = f"{head}  ({', '.join(extras)})"
    return _clip(head, width)


def _clip(text: str, width: int) -> str:
    width = max(1, width)
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return f"{text[: width - 3]}..."


class QueuePanel(VerticalScroll):
    """A reconciled list of queued inputs."""

    DEFAULT_CSS = QUEUE_PANEL_CSS

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._widgets: dict[str, Static] = {}
        self._order: tuple[str, ...] = ()

    @property
    def rows(self) -> int:
        return len(self._widgets)

    @property
    def order(self) -> tuple[str, ...]:
        return self._order

    def row_widget(self, message_id: str) -> Static | None:
        return self._widgets.get(message_id)

    def row_text(self, message_id: str) -> str:
        widget = self._widgets.get(message_id)
        if widget is None:
            return ""
        content = widget.content
        return str(getattr(content, "plain", "") or "")

    def show(self, items: Sequence[PendingInputData], *, width: int) -> None:
        """Render ``items`` in order, updating existing rows in place."""
        wanted = {item.message_id for item in items}
        for message_id in [key for key in self._widgets if key not in wanted]:
            widget = self._widgets.pop(message_id)
            widget.remove()
        for index, item in enumerate(items, start=1):
            text = queue_row(item, position=index, width=width)
            widget = self._widgets.get(item.message_id)
            if widget is None:
                widget = Static(Text(text), classes="queue-row")
                self._widgets[item.message_id] = widget
                self.mount(widget)
            else:
                widget.update(Text(text))
        self._order = tuple(item.message_id for item in items)
        self._reorder()

    def _reorder(self) -> None:
        for index, message_id in enumerate(self._order):
            widget = self._widgets.get(message_id)
            if widget is None:
                continue
            if index < len(self.children) and self.children[index] is widget:
                continue
            self.move_child(widget, before=index)


__all__ = ["QUEUE_PANEL_CSS", "QueuePanel", "queue_row"]
