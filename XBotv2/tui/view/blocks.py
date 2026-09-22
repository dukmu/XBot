"""Clamped blocks: a fixed-height window over content that may be huge.

The transcript is a conversation, not a log viewer. A tool result, a reasoning
trace or a long answer therefore renders as a block with a maximum height:
collapsed it shows a summary row and a short preview, expanded it shows the
content inside its own scroll area. Either way the transcript below it keeps its
place, which is what "does not flood the screen" means here.

What a block shows is a pure function of the text, so it is tested without a
terminal; the widget only mounts what that function returns.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static

#: The most lines a block may occupy, expanded or not.
BLOCK_MAX_LINES = 12
#: How many lines a collapsed block previews.
BLOCK_PREVIEW_LINES = 2

BLOCK_CSS = f"""
ClampedBlock {{
    height: auto;
    max-height: {BLOCK_MAX_LINES};
    width: 1fr;
}}
ClampedBlock:focus {{
    border-left: thick $accent;
}}
ClampedBlock > .block-head {{
    height: auto;
    width: 1fr;
    color: $text-muted;
}}
ClampedBlock > .block-body {{
    height: auto;
    width: 1fr;
}}
"""


@dataclass(frozen=True)
class BlockPlan:
    """What to render for one block right now."""

    head: str
    body: str
    lines: int
    collapsible: bool
    streaming: bool = False


def count_lines(text: str) -> int:
    """Visible lines in ``text``; a trailing newline is not another line."""
    if not text:
        return 0
    return len(text.rstrip("\n").splitlines())


def plan_block(
    text: str,
    *,
    label: str,
    expanded: bool = False,
    streaming: bool = False,
    max_lines: int = BLOCK_MAX_LINES,
    preview_lines: int = BLOCK_PREVIEW_LINES,
) -> BlockPlan:
    """Decide the summary row and the body for one piece of content.

    Content that fits is shown whole and gets no summary row at all -- a header
    saying "3 lines" would be noise. Content that does not fit is collapsed
    unless the reader expanded it; a *streaming* body is always shown to its end,
    because that is the part being written.
    """
    lines = count_lines(text)
    if lines <= max_lines:
        return BlockPlan(head="", body=text, lines=lines, collapsible=False)
    if streaming or expanded:
        return BlockPlan(
            head=_head(label, lines, expanded=True, streaming=streaming),
            body=text,
            lines=lines,
            collapsible=True,
            streaming=streaming,
        )
    preview = "\n".join(text.rstrip("\n").splitlines()[:preview_lines])
    return BlockPlan(
        head=_head(label, lines, expanded=False, streaming=False),
        body=preview,
        lines=lines,
        collapsible=True,
    )


def _head(label: str, lines: int, *, expanded: bool, streaming: bool) -> str:
    """The summary row, including the key that folds and unfolds the block.

    It names a key that works from wherever the reader's focus is (the composer,
    most of the time): promising "Enter" would be a lie while the input owns the
    keyboard.
    """
    marker = "▾" if expanded else "▸"
    action = "collapses" if expanded else "expands"
    if streaming:
        return f"{marker} {label} · {lines} lines streaming"
    return f"{marker} {label} · {lines} lines · ctrl+e {action}"


class ClampedBlock(VerticalScroll):
    """One fixed-height block of long content."""

    DEFAULT_CSS = BLOCK_CSS
    can_focus = True
    BINDINGS = [
        Binding("enter", "toggle", "Expand or collapse", show=False),
        Binding("space", "toggle", "Expand or collapse", show=False),
    ]

    def __init__(
        self,
        text: str,
        *,
        label: str,
        renderable: Text | object | None = None,
        expanded: bool = False,
        streaming: bool = False,
        max_lines: int = BLOCK_MAX_LINES,
        preview_lines: int = BLOCK_PREVIEW_LINES,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.label = label
        self._text = text
        self.max_lines = max_lines
        self.preview_lines = preview_lines
        self._plan = plan_block(
            text,
            label=label,
            expanded=expanded,
            streaming=streaming,
            max_lines=max_lines,
            preview_lines=preview_lines,
        )
        self._expanded = expanded
        self._renderable = renderable
        self.head_widget: Static | None = None
        self.body_widget: Static | None = None

    # --- what this block is -------------------------------------------
    @property
    def expanded(self) -> bool:
        return self._expanded

    @property
    def collapsible(self) -> bool:
        return self._plan.collapsible

    @property
    def lines(self) -> int:
        return self._plan.lines

    @property
    def plan(self) -> BlockPlan:
        return self._plan

    @property
    def shown_text(self) -> str:
        """The body text on screen right now: the preview, or the whole thing."""
        return _plain(self.body_widget)

    @property
    def head_text(self) -> str:
        return _plain(self.head_widget)

    def compose(self) -> ComposeResult:
        if self._plan.head:
            self.head_widget = Static(Text(self._plan.head), classes="block-head")
            yield self.head_widget
        self.body_widget = Static(
            Text(self._plan.body) if isinstance(self._renderable, Text) else (self._renderable or Text(self._plan.body)),
            classes="block-body",
        )
        yield self.body_widget

    # --- changing it ---------------------------------------------------
    def action_toggle(self) -> None:
        self.toggle()

    def toggle(self) -> None:
        if not self._plan.collapsible:
            return
        self._expanded = not self._expanded
        self._replan()
        self._apply()

    def show(
        self,
        text: str,
        *,
        renderable: Text | object | None = None,
        streaming: bool = False,
    ) -> None:
        """Re-apply content: the same block, updated in place."""
        self._text = text
        self._renderable = renderable
        if streaming:
            # Watching an answer arrive means reading it to its end; folding it
            # away the moment the turn finishes would take it from the reader.
            self._expanded = True
        self._replan(streaming=streaming)
        self._apply()
        if streaming:
            self.scroll_end(animate=False, immediate=True)

    def on_click(self) -> None:
        self.focus()
        self.toggle()

    def _replan(self, *, streaming: bool = False) -> None:
        self._plan = plan_block(
            self._text,
            label=self.label,
            expanded=self._expanded,
            streaming=streaming,
            max_lines=self.max_lines,
            preview_lines=self.preview_lines,
        )

    def _apply(self) -> None:
        head_text = Text(self._plan.head)
        body_text = (
            self._renderable
            if (self._expanded and self._renderable is not None)
            else Text(self._plan.body)
        )
        if self._plan.head:
            if self.head_widget is None:
                self.head_widget = Static(head_text, classes="block-head")
                self.mount(self.head_widget, before=0)
            else:
                self.head_widget.update(head_text)
        elif self.head_widget is not None:
            head = self.head_widget
            self.head_widget = None
            head.remove()
        if self.body_widget is not None:
            self.body_widget.update(body_text)
        self.set_class(self._plan.collapsible, "collapsible")
        self.set_class(self._expanded, "expanded")


def _plain(widget: Static | None) -> str:
    if widget is None:
        return ""
    content = widget.content
    return str(getattr(content, "plain", None) or getattr(content, "markup", "") or "")


__all__ = [
    "BLOCK_CSS",
    "BLOCK_MAX_LINES",
    "BLOCK_PREVIEW_LINES",
    "BlockPlan",
    "ClampedBlock",
    "count_lines",
    "plan_block",
]
