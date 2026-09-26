"""Clamped blocks: a content-sized window with a fixed maximum height.

The transcript is a conversation, not a log viewer. A tool result, a reasoning
trace or a long answer therefore renders as a block with a maximum height:
collapsed it shows a summary row and a short preview, expanded it grows only to
its content height and then scrolls at the maximum. This keeps short thinking
and tool details compact while huge output cannot flood the transcript.

What a block shows is a pure function of the text, so it is tested without a
terminal; the widget only mounts what that function returns.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static

#: The most rows the complete block may occupy, expanded or not.
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
    expanded: bool | None = None,
    streaming: bool = False,
    always_collapsible: bool = False,
    max_lines: int = BLOCK_MAX_LINES,
    preview_lines: int = BLOCK_PREVIEW_LINES,
    inline_label: bool = False,
) -> BlockPlan:
    """Decide the summary row and the body for one piece of content.

    Content that fits is shown whole and gets no summary row at all -- a header
    saying "3 lines" would be noise. Content that does not fit is collapsed
    unless the reader expanded it. By default a streaming body follows its end;
    passing ``expanded=False`` lets the reader fold it while it is arriving.
    """
    lines = count_lines(text)
    if lines <= max_lines and not always_collapsible:
        return BlockPlan(head="", body=text, lines=lines, collapsible=False)
    show_all = streaming if expanded is None else expanded
    if show_all:
        return BlockPlan(
            head=_head(
                label, lines, expanded=True, streaming=streaming,
                inline_label=inline_label,
            ),
            body=text,
            lines=lines,
            collapsible=True,
            streaming=streaming,
        )
    preview = "\n".join(text.rstrip("\n").splitlines()[:preview_lines])
    return BlockPlan(
        head=_head(
            label, lines, expanded=False, streaming=streaming,
            inline_label=inline_label,
        ),
        body=preview,
        lines=lines,
        collapsible=True,
        streaming=streaming,
    )


def _head(
    label: str,
    lines: int,
    *,
    expanded: bool,
    streaming: bool,
    inline_label: bool,
) -> str:
    """The summary row, including the key that folds and unfolds the block.

    It names a key that works from wherever the reader's focus is (the composer,
    most of the time): promising "Enter" would be a lie while the input owns the
    keyboard.
    """
    marker = "▾" if expanded else "▸"
    action = "collapses" if expanded else "expands"
    prefix = label if inline_label else f"{marker} {label}"
    count = f"{lines} {'line' if lines == 1 else 'lines'}"
    if streaming:
        return f"{prefix} · {count} streaming · ctrl+e {action}"
    return f"{prefix} · {count} · ctrl+e {action}"


class ClampedBlock(VerticalScroll):
    """One content-sized block, clamped to a fixed maximum height."""

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
        collapse_after_streaming: bool = False,
        always_show_label: bool = False,
        always_collapsible: bool = False,
        max_lines: int = BLOCK_MAX_LINES,
        preview_lines: int = BLOCK_PREVIEW_LINES,
        inline_label: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.label = label
        self.always_show_label = always_show_label
        self.always_collapsible = always_collapsible
        self.collapse_after_streaming = collapse_after_streaming
        self._text = text
        self.max_lines = max_lines
        self.preview_lines = preview_lines
        self.inline_label = inline_label
        self._expanded = expanded or streaming
        self._manual_expanded: bool | None = True if expanded else None
        self._plan = plan_block(
            text,
            label=label,
            expanded=self._expanded,
            streaming=streaming,
            always_collapsible=always_collapsible,
            max_lines=max_lines,
            preview_lines=preview_lines,
            inline_label=inline_label,
        )
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
        head = self._head_text()
        if head:
            self.head_widget = Static(Text(head), classes="block-head")
            yield self.head_widget
        self.body_widget = Static(
            self._renderable
            if (
                self._renderable is not None
                and (self._expanded or not self._plan.collapsible)
            )
            else Text(self._plan.body),
            classes="block-body",
        )
        self.body_widget.display = bool(self._plan.body)
        yield self.body_widget

    # --- changing it ---------------------------------------------------
    def action_toggle(self) -> None:
        self.toggle()

    def toggle(self) -> None:
        if not self._plan.collapsible:
            return
        self._expanded = not self._expanded
        self._manual_expanded = self._expanded
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
        was_streaming = self._plan.streaming
        self._text = text
        self._renderable = renderable
        if streaming and self._manual_expanded is None:
            self._expanded = True
        elif (
            was_streaming
            and not streaming
            and self.collapse_after_streaming
            and self._manual_expanded is None
        ):
            self._expanded = False
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
            always_collapsible=self.always_collapsible,
            max_lines=self.max_lines,
            preview_lines=self.preview_lines,
            inline_label=self.inline_label,
        )

    def _apply(self) -> None:
        head_text = Text(self._head_text())
        body_text = (
            self._renderable
            if (
                self._renderable is not None
                and (self._expanded or not self._plan.collapsible)
            )
            else Text(self._plan.body)
        )
        if self._head_text():
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
            self.body_widget.display = bool(self._plan.body)
        self.set_class(self._plan.collapsible, "collapsible")
        self.set_class(self._expanded and self._plan.collapsible, "expanded")

    def _head_text(self) -> str:
        return self._plan.head or (self.label if self.always_show_label else "")


def _plain(widget: Static | None) -> str:
    if widget is None:
        return ""
    return str(widget.content)


__all__ = [
    "BLOCK_CSS",
    "BLOCK_MAX_LINES",
    "BLOCK_PREVIEW_LINES",
    "BlockPlan",
    "ClampedBlock",
    "count_lines",
    "plan_block",
]
