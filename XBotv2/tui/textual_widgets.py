"""Textual widgets and render helpers for the protocol TUI."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from textwrap import shorten, wrap
from typing import Any

from pydantic import JsonValue
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual import events
from textual.message import Message
from textual.events import Key
from textual.widgets import Collapsible, Static, TextArea

from XBotv2.tui.client import TuiMessage, TuiState, TuiTask, TuiTool, format_value

# Bound Markdown render cache: streaming assistant deltas re-render the same
# growing content many times per second; memoizing the parsed render avoids
# re-tokenizing the whole message on every 50ms tick. Keyed by role+content
# so an appended delta (content change) naturally misses and re-renders once.
_MARKDOWN_CACHE: OrderedDict[tuple[str, str], Text | Markdown] = OrderedDict()
_MARKDOWN_CACHE_MAX = 64

# Plain-text render of a Markdown body, used for copying text out of the TUI
# (keyboard fallback; Textual's built-in extraction only handles Text/Content
# visuals, so this renders Markdown at a given width itself).
_PLAIN_CACHE: OrderedDict[tuple[int, str], str] = OrderedDict()
_PLAIN_CACHE_MAX = 64


def _cached_render_message(content: str, *, role: str) -> Text | Markdown:
    key = (role, content)
    cached = _MARKDOWN_CACHE.get(key)
    if cached is not None:
        _MARKDOWN_CACHE.move_to_end(key)
        return cached
    rendered = _render_message_uncached(content, role=role)
    _MARKDOWN_CACHE[key] = rendered
    if len(_MARKDOWN_CACHE) > _MARKDOWN_CACHE_MAX:
        _MARKDOWN_CACHE.popitem(last=False)
    return rendered


def _markdown_plain_text(content: str, width: int) -> str:
    """Render Markdown to plain text for clipboard extraction."""
    key = (width, content)
    cached = _PLAIN_CACHE.get(key)
    if cached is not None:
        _PLAIN_CACHE.move_to_end(key)
        return cached
    from rich.console import Console

    console = Console(
        force_terminal=False,
        color_system=None,
        legacy_windows=False,
        width=max(10, width or 80),
    )
    with console.capture() as capture:
        console.print(Markdown(content))
    plain = "\n".join(line.rstrip() for line in capture.get().splitlines()).rstrip()
    _PLAIN_CACHE[key] = plain
    if len(_PLAIN_CACHE) > _PLAIN_CACHE_MAX:
        _PLAIN_CACHE.popitem(last=False)
    return plain


_STATUS_BADGE_STYLE: dict[str, str] = {
    "Ready": "green",
    "Running": "yellow",
    "Thinking": "cyan",
    "Connecting": "yellow",
    "Waiting for user": "cyan",
    "Approval required": "magenta",
    "Permission denied": "red",
    "Interrupted": "yellow",
    "Error": "red",
    "Shutdown": "dim",
}


def status_renderable(
    *,
    status: str,
    session_id: str,
    thread_id: str,
    workspace_root: str,
    provider: str,
    model: str,
    agent_name: str = "",
    model_mode: str = "",
    status_slots: dict[str, str] | None = None,
    context_window: int,
    context_input_tokens: int,
    activity: str,
    queue_depth: int,
    usage: dict[str, int],
    width: int,
) -> Text:
    """Build a compact bottom status line from protocol-owned state."""

    width = max(20, width)
    style = _STATUS_BADGE_STYLE.get(status, "white")
    workspace = Path(workspace_root).name if workspace_root else ""
    total = usage["total_tokens"]
    queue_label = f"q:{queue_depth}" if width < 32 else f"queued:{queue_depth}"
    token_label = (
        f"t:{_compact_count(total)}" if width < 32 else f"tokens:{_compact_count(total)}"
    )
    required: list[tuple[str, str]] = [(token_label, "")]
    if queue_depth:
        required.insert(0, (queue_label, "yellow"))
    if context_window > 0 and context_input_tokens > 0 and width >= 38:
        remaining = round(
            100 * max(0, context_window - context_input_tokens) / context_window
        )
        required.append((f"ctx-free:{remaining}%", "cyan"))
    status_width = width - _segments_width(required) - 2
    segments = [(_clip_label(status, status_width), style), *required]

    has_brand = _segments_width([("XBotv2", "bold"), *segments]) <= width
    if has_brand:
        segments.insert(0, ("XBotv2", "bold"))
    activity_index = 2 if has_brand else 1
    with_activity = [*segments]
    with_activity.insert(activity_index, (activity, ""))
    if _segments_width(with_activity) <= width:
        segments.insert(activity_index, (activity, ""))

    if width >= 80:
        # "in" is the full prompt sent to the provider, including cache I/O;
        # deepseek reports uncached input as 0 when everything is cached.
        full_input = (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("prompt_cache_write_tokens", 0)
        )
        detailed_tokens = (
            f"tokens:{_compact_count(total)} "
            f"({_compact_count(full_input)} in / "
            f"{_compact_count(usage['output_tokens'])} out)"
        )
        token_index = next(
            index for index, (label, _style) in enumerate(segments)
            if label == token_label
        )
        with_detailed_tokens = [*segments]
        with_detailed_tokens[token_index] = (detailed_tokens, "")
        if _segments_width(with_detailed_tokens) <= width:
            segments[token_index] = (detailed_tokens, "")

    optional: list[tuple[str, str]] = []
    if agent_name:
        optional.append((f"agent:{agent_name[:20]}", "blue"))
    model_identity = "/".join(part for part in (provider, model) if part)
    if model_identity:
        if model_mode:
            model_identity = f"{model_identity}:{model_mode}"
        optional.append((model_identity[:40], "green"))
    for name, value in (status_slots or {}).items():
        optional.append((f"{name}:{value}"[:30], "magenta"))
    if workspace:
        optional.append((f"cwd:{workspace[:20]}", "cyan"))
    if width >= 120:
        session = session_id if thread_id == "agent" else f"{session_id}/{thread_id}"
        optional.append((f"session:{session}", "dim"))
    for candidate in optional:
        if _segments_width([*segments, candidate]) <= width:
            segments.append(candidate)

    text = Text()
    for label, segment_style in segments:
        if text.plain:
            text.append("  ", style="dim")
        text.append(label, style=segment_style)
    return text


def _segments_width(segments: list[tuple[str, str]]) -> int:
    return sum(len(label) for label, _style in segments) + 2 * max(0, len(segments) - 1)


def _clip_label(label: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(label) <= width:
        return label
    if width <= 3:
        return label[:width]
    return f"{label[:width - 3]}..."


def _compact_count(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}k"
    return f"{value / 1_000_000:.1f}M"


def tasks_renderable(tasks: list[TuiTask], *, width: int) -> Text:
    """Render compact task rows from authoritative task snapshots."""
    text = Text()
    show_details = len(tasks) <= 3
    for task in tasks:
        marker, style = {
            "pending": ("-", "yellow"),
            "running": (spinner(int(time.monotonic() * 2)), "yellow"),
            "completed": ("done", "green"),
            "failed": ("failed", "red"),
            "stopped": ("stopped", "dim"),
        }.get(task.status, (task.status, "white"))
        kind = "agent" if task.kind == "agent" else "shell"
        summary_width = max(
            12, width - len(task.task_id) - len(marker) - len(kind) - 15
        )
        command = shorten(task.command, width=summary_width, placeholder="...")
        if text.plain:
            text.append("\n")
        text.append(f"{marker:>7}  ", style=style)
        text.append(f"{task.task_id}  ", style="cyan")
        text.append(f"{kind}  ", style="magenta" if kind == "agent" else "blue")
        text.append(command)
        text.append(f"  {task.elapsed():.1f}s", style="dim")
        detail = ""
        if show_details:
            detail = task.error or (
                task.output.strip() if task.status == "completed" else ""
            )
        if detail:
            text.append("\n         ")
            text.append(
                shorten(detail, width=max(12, width - 9), placeholder="..."),
                style="dim",
            )
    return text


class SubagentTaskWidget(Collapsible):
    """One expandable task with the full command/details behind a bounded window."""

    def __init__(
        self,
        task: TuiTask,
        *,
        width: int,
        collapsed: bool = True,
    ) -> None:
        self.task_id = task.task_id
        super().__init__(
            BoundedText(task_detail_text(task), classes="task-detail"),
            title=_task_title(task, width=width),
            collapsed=collapsed,
            classes="subagent-task",
        )


def task_detail_text(task: TuiTask) -> str:
    """The full task record, unwrapped: the compact row never truncates it."""
    parts: list[str] = []
    if task.command:
        parts.append(f"command: {task.command}")
    if task.cwd:
        parts.append(f"cwd: {task.cwd}")
    meta = [f"kind: {task.kind}"]
    if task.agent:
        meta.append(f"agent: {task.agent}")
    meta.append(f"status: {task.status}")
    if task.thread_id:
        meta.append(f"thread: {task.thread_id}")
    usage = task.usage
    if usage:
        total = int(usage.get("total_tokens") or 0)
        if total:
            meta.append(f"tokens: {_compact_count(total)}")
    parts.append("  ".join(meta))
    if task.output.strip():
        parts.append("output:")
        parts.append(task.output.rstrip())
    if task.error.strip():
        parts.append("error:")
        parts.append(task.error.rstrip())
    return "\n".join(parts)


class TaskListWidget(VerticalScroll):
    """Scrollable task list with nested subagent details."""

    can_focus = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._signature: tuple[Any, ...] = ()

    def update_tasks(self, tasks: list[TuiTask], *, width: int) -> None:
        signature = tuple(
            (
                task.task_id,
                task.status,
                task.output,
                task.error,
                task.thread_id,
                tuple(sorted(task.usage.items())),
                int(time.monotonic() * 2)
                if task.status in {"pending", "running"}
                else 0,
            )
            for task in tasks
        )
        if signature == self._signature:
            return
        expanded = {
            widget.task_id
            for widget in self.query(SubagentTaskWidget)
            if not widget.collapsed
        }
        self._signature = signature
        self.remove_children()
        widgets: list[SubagentTaskWidget] = [
            SubagentTaskWidget(
                task,
                width=width,
                collapsed=task.task_id not in expanded,
            )
            for task in tasks
        ]
        if widgets:
            self.mount(*widgets)


def _task_title(task: TuiTask, *, width: int) -> str:
    marker = {
        "pending": "-",
        "running": "running",
        "completed": "done",
        "failed": "failed",
        "stopped": "stopped",
    }.get(task.status, task.status)
    agent = task.agent or task.command.partition(":")[0] or "subagent"
    available = max(12, width - len(task.task_id) - len(marker) - len(agent) - 8)
    prompt = task.command.partition(":")[2].strip() or task.command
    return (
        f"{marker}  {task.task_id}  {agent}  "
        f"{shorten(prompt, width=available, placeholder='...')}"
    )


def queue_renderable(messages: list[str], *, width: int) -> Text:
    text = Text()
    for index, message in enumerate(messages[:3], start=1):
        if text.plain:
            text.append("\n")
        text.append(f"{index:>2}  ", style="yellow")
        text.append(
            shorten(message.replace("\n", " "), width=max(12, width - 4), placeholder="...")
        )
    if len(messages) > 3:
        text.append(f"\n    +{len(messages) - 3} more", style="dim")
    return text


class ComposerTextArea(TextArea):
    """Multiline composer with submit, history, and slash completion keys."""

    async def _on_key(self, event: Key) -> None:
        app = self.app
        if hasattr(app, "submit_composer"):
            if app._choice_mode_active():
                event.stop()
                event.prevent_default()
                return
            popup = app._get_completion_popup()
            popup_visible = popup is not None and popup.visible
            if event.key == "enter":
                event.stop()
                event.prevent_default()
                await app.submit_composer()
                return
            if event.key == "shift+enter":
                event.stop()
                event.prevent_default()
                self.insert("\n")
                return
            if event.key == "tab" and popup_visible and popup is not None:
                spec = popup.current_match()
                if spec is not None:
                    event.stop()
                    event.prevent_default()
                    app._accept_completion(spec)
                    return
            if event.key == "tab":
                # Leaving the input moves focus into the transcript, where the
                # arrow keys scroll what is under the cursor.
                event.stop()
                event.prevent_default()
                self.screen.focus_next()
                return
            if event.key == "up" and popup_visible and popup is not None:
                event.stop()
                event.prevent_default()
                popup.move_selection(-1)
                return
            if event.key == "down" and popup_visible and popup is not None:
                event.stop()
                event.prevent_default()
                popup.move_selection(1)
                return
            if event.key == "escape" and popup_visible and popup is not None:
                event.stop()
                event.prevent_default()
                app._dismiss_completion_popup()
                return
            if event.key == "up" and (not self.text.strip() or app._history_index is not None):
                event.stop()
                event.prevent_default()
                app.history_previous()
                return
            if event.key == "down" and app._history_index is not None:
                event.stop()
                event.prevent_default()
                app.history_next()
                return
            if event.key in {"pageup", "pagedown"}:
                event.stop()
                event.prevent_default()
                app.scroll_transcript_page(down=event.key == "pagedown")
                return
        await super()._on_key(event)


class TranscriptScroll(VerticalScroll):
    """Transcript that scrolls with the wheel or with the keyboard.

    Scrolling is the same act either way: the wheel over the transcript, an
    arrow key while it holds focus, and a focused block reaching its end all
    move this viewport (see :class:`BoundedText`).  Emits
    :class:`ReplayTopReached` when the user scrolls to the top while older
    replayed history is still unmounted, so the app can lazy-load it.  Emits
    :class:`Scrolled` after any user-initiated scroll so the app can track
    whether it should keep following the live tail, and :class:`HeightChanged`
    when the viewport height changes (e.g. the slash-completion popup appears),
    so the app can re-pin a follower to the bottom instead of leaving it
    stranded mid-scroll.
    """

    can_focus = True

    class ReplayTopReached(Message):
        pass

    class ReplayBottomReached(Message):
        pass

    class Scrolled(Message):
        """Posted after a user scroll; ``at_end`` is the resulting state."""

        def __init__(self, at_end: bool) -> None:
            self.at_end = at_end
            super().__init__()

    class HeightChanged(Message):
        pass

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._last_height: int | None = None

    def on_resize(self, event: events.Resize) -> None:
        if self._last_height is not None and event.size.height != self._last_height:
            self.post_message(self.HeightChanged())
        self._last_height = event.size.height

    def _post_scrolled(self) -> None:
        self.post_message(self.Scrolled(at_end=self.is_vertical_scroll_end))

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        super()._on_mouse_scroll_up(event)
        if self.scroll_y == 0:
            self.post_message(self.ReplayTopReached())
        self._post_scrolled()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        super()._on_mouse_scroll_down(event)
        if self.is_vertical_scroll_end:
            self.post_message(self.ReplayBottomReached())
        self._post_scrolled()

    def scroll_up(self, *args, **kwargs) -> None:
        super().scroll_up(*args, **kwargs)
        if self.scroll_y == 0:
            self.post_message(self.ReplayTopReached())
        self._post_scrolled()

    def scroll_down(self, *args, **kwargs) -> None:
        super().scroll_down(*args, **kwargs)
        if self.is_vertical_scroll_end:
            self.post_message(self.ReplayBottomReached())
        self._post_scrolled()

    def scroll_page_up(self, *args, **kwargs) -> None:
        super().scroll_page_up(*args, **kwargs)
        # A page jump settles on the next refresh, so report the resulting
        # position then instead of reading a stale offset here.
        self.call_after_refresh(self._post_page_scroll)

    def scroll_page_down(self, *args, **kwargs) -> None:
        super().scroll_page_down(*args, **kwargs)
        self.call_after_refresh(self._post_page_scroll)

    def scroll_home(self, *args, **kwargs) -> None:
        super().scroll_home(*args, **kwargs)
        self.call_after_refresh(self._post_page_scroll)

    def _post_page_scroll(self) -> None:
        if self.scroll_y <= 0:
            self.post_message(self.ReplayTopReached())
        if self.is_vertical_scroll_end:
            self.post_message(self.ReplayBottomReached())
        self._post_scrolled()


BLOCK_MAX_ROWS = 9
"""Rows one reasoning or tool-detail block may occupy before it windows."""

# Only used to ask Rich for wrapped rows; the width is always passed in.
_MEASURE_CONSOLE = Console(width=80)


class BlockStep(Static):
    """A tap target that scrolls the block it belongs to (no keyboard needed)."""

    def __init__(self, mark: str, block: "BoundedText", direction: int, *, classes: str) -> None:
        super().__init__(mark, classes=classes)
        self._block = block
        self._direction = direction

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self._block.scroll_screen(self._direction)


class BoundedText(Vertical):
    """A fixed-height window over the wrapped text of one collapsible block.

    A long reasoning trace or tool result is rendered a window of rows at a
    time, so one block can never take over the transcript and never costs a
    full-document render. The window follows the tail while the text streams;
    afterwards the wheel over the block, the tap marks in its footer, or the
    arrow keys while the block holds focus scroll it. At either end of the text
    the scroll is left alone, so the focused block hands the keystroke to the
    transcript instead of trapping the reader.
    """

    can_focus = True

    def __init__(
        self,
        content: Any = "",
        *,
        classes: str = "",
        id: str | None = None,
        max_rows: int = BLOCK_MAX_ROWS,
    ) -> None:
        super().__init__(classes=classes, id=id)
        # max_rows=0 asks for a pane-sized window: the block fills its widget.
        self._full_height = max_rows == 0
        self._max_rows = 24 if self._full_height else max(1, max_rows)
        self._width_cache = 0
        self._row_cache: dict[tuple[int, int], list[Text]] = {}
        self._plain = ""
        self._lines: list[str] = [""]
        self._cursor = (0, 0)  # first visible (logical line, wrapped row)
        self._follow = True
        self._style = "default"
        self._window = Static(Text(""), classes="block-window")
        self._up = BlockStep("▲", self, -1, classes="block-step up")
        self._down = BlockStep("▼", self, 1, classes="block-step down")
        self._counter = Static("", classes="block-counter")
        self._foot = Horizontal(self._up, self._counter, self._down, classes="block-foot")
        if content:
            self.update(content)

    def compose(self):
        yield self._window
        yield self._foot

    @property
    def text(self) -> str:
        """The complete block text, independent of the visible window."""
        return self._plain

    @property
    def window_text(self) -> str:
        """The wrapped rows currently rendered."""
        return "\n".join(row.plain for row in self._visible_rows())

    @property
    def line_count(self) -> int:
        return len(self._lines)

    @property
    def max_rows(self) -> int:
        """Rows this block may render, scaled to the current screen."""
        return self._max_rows

    @property
    def window_range(self) -> tuple[int, int]:
        """1-based inclusive range of the logical lines the window touches."""
        start = self._cursor[0]
        end = start
        seen = 0
        width = self._width()
        while end < len(self._lines) and seen < self._max_rows:
            seen += len(self._rows(end, width))
            end += 1
        return (start + 1, max(end, start + 1))

    @property
    def at_end(self) -> bool:
        """Whether the last row of the text is inside the current window."""
        return self._last_row_visible()

    def update(self, renderable: Any = "") -> None:
        """Replace the block text, keeping the window and tail-follow state."""
        if isinstance(renderable, Text):
            text = renderable.plain
            self._style = str(renderable.style or "default")
        else:
            text = "" if renderable is None else str(renderable)
        previous = self._plain
        if text == previous:
            # A refresh that re-sends the same accumulated text is a no-op:
            # resetting the window here would yank a streaming block back to
            # its head and defeat tail-following.
            return
        # Streaming re-sends the whole accumulated text; only the appended tail
        # is re-split so a growing block stays cheap to update.
        tail = min(64, len(previous))
        growing = (
            bool(previous)
            and len(text) > len(previous)
            and text.startswith(previous[:64])
            and text[len(previous) - tail:len(previous)] == previous[-tail:]
        )
        if growing:
            added = text[len(previous):].split("\n")
            self._lines[-1] += added[0]
            self._lines.extend(added[1:])
            self._row_cache = {
                key: rows for key, rows in self._row_cache.items() if key[0] < len(self._lines) - 1
            }
        else:
            self._lines = text.split("\n")
            self._row_cache.clear()
        self._plain = text
        if growing:
            if self._follow:
                self._cursor = self._tail_cursor()
        else:
            self._cursor = (0, 0)
            self._follow = True
        self._render_window()

    def append(self, suffix: str) -> None:
        """Grow the block text without re-joining the whole content.

        Used by live viewers (a thread's event stream) where each frame is a
        fresh chunk; the window follows the tail unless the reader scrolled
        back into the history.
        """
        if not suffix:
            return
        if not self._plain:
            self.update(suffix)
            return
        newline = self._plain.endswith("\n")
        added = suffix.split("\n")
        if newline:
            self._lines[-1] = added[0]
        else:
            self._lines[-1] += added[0]
        self._lines.extend(added[1:])
        self._plain += suffix
        self._row_cache = {
            key: rows
            for key, rows in self._row_cache.items()
            if key[0] < len(self._lines) - 1
        }
        if self._follow:
            self._cursor = self._tail_cursor()
        self._render_window()

    def scroll_screen(self, direction: int) -> bool:
        """Scroll this block by one screenful in ``direction``."""
        return self.scroll_rows(direction * max(1, self._max_rows - 1))

    def scroll_rows(self, rows: int) -> bool:
        """Scroll the window by ``rows`` rendered rows; whether it moved."""
        cursor = self._cursor
        step = 1 if rows > 0 else -1
        for _ in range(abs(rows)):
            moved = self._step(step)
            if not moved:
                break
        if self._cursor == cursor:
            return False
        self._follow = self.at_end
        self._render_window()
        return True

    # --- row geometry -------------------------------------------------
    def _width(self) -> int:
        # ``size`` is 0 until the first layout pass; the resize event carries the
        # real width and re-renders the window.
        return max(4, (self._width_cache or self.size.width) - 2)

    def _rows(self, line_index: int, width: int) -> list[Text]:
        """Wrapped rows of one logical line, memoized for the visible window."""
        key = (line_index, width)
        rows = self._row_cache.get(key)
        if rows is not None:
            return rows
        text = Text(self._lines[line_index], style=self._style, no_wrap=False, justify="left")
        rows = [Text(row.plain.rstrip(), style=self._style) for row in text.wrap(_MEASURE_CONSOLE, width)]
        if len(self._row_cache) > 32:
            self._row_cache.clear()
        self._row_cache[key] = rows or [Text("", style=self._style)]
        return self._row_cache[key]

    def _step(self, direction: int) -> bool:
        line, row = self._cursor
        if direction > 0:
            rows = self._rows(line, self._width())
            if row + 1 < len(rows):
                self._cursor = (line, row + 1)
                return True
            if line + 1 >= len(self._lines):
                return False
            self._cursor = (line + 1, 0)
            return True
        if row > 0:
            self._cursor = (line, row - 1)
            return True
        if line == 0:
            return False
        previous = self._rows(line - 1, self._width())
        self._cursor = (line - 1, max(0, len(previous) - 1))
        return True

    def _tail_cursor(self) -> tuple[int, int]:
        """Cursor that shows the newest rows of the block."""
        cursor = self._cursor
        self._cursor = (max(0, len(self._lines) - 1), 0)
        for _ in range(self._max_rows - 1):
            if not self._step(-1):
                break
        tail = self._cursor
        self._cursor = cursor
        return tail

    def _last_row_visible(self) -> bool:
        """Whether the end of the text fits in the window from the cursor on."""
        rows = 0
        line, row = self._cursor
        width = self._width()
        while line < len(self._lines):
            remaining = len(self._rows(line, width)) - row
            if remaining > self._max_rows - rows:
                return False
            rows += remaining
            line += 1
            row = 0
        return True

    def _visible_rows(self) -> list[Text]:
        rows: list[Text] = []
        line, row = self._cursor
        width = self._width()
        while line < len(self._lines) and len(rows) < self._max_rows:
            wrapped = self._rows(line, width)
            rows.extend(wrapped[row:row + (self._max_rows - len(rows))])
            line += 1
            row = 0
        return rows

    def _render_window(self) -> None:
        self._max_rows = self._budget()
        rows = self._visible_rows()
        self._window.update(Text("\n").join(rows) if rows else Text(""))
        # The row count changes as the window moves; ask for a layout pass.
        self.refresh(layout=True)
        first, last = self.window_range
        total = len(self._lines)
        counter = (
            f"{first}–{last} of {total} lines"
            if total > 1 and not self._whole_block_visible()
            else ""
        )
        self._counter.update(counter)
        self._up.display = self._cursor != (0, 0)
        self._down.display = not self._last_row_visible()
        # No empty row under a block that fits entirely.
        self._foot.display = self._up.display or self._down.display or bool(counter)

    def _whole_block_visible(self) -> bool:
        return self._cursor == (0, 0) and self._last_row_visible()

    def _budget(self) -> int:
        """Rows one block may use: the pane, or at most a quarter of a screen."""
        try:
            height = self.size.height if self._full_height else self.screen.size.height
        except Exception:  # noqa: BLE001 — not mounted yet
            return 24 if self._full_height else BLOCK_MAX_ROWS
        if self._full_height:
            return max(4, height - 2)  # window rows plus the footer
        return max(3, min(BLOCK_MAX_ROWS, height // 4)) if height > 0 else BLOCK_MAX_ROWS

    def on_mount(self) -> None:
        # The block is filled before it has a screen; lay out the window once
        # the real width and height are known.
        self._render_window()

    def on_resize(self, event: events.Resize) -> None:
        changed = event.size.width != self._width_cache or self._budget() != self._max_rows
        self._width_cache = event.size.width
        if changed:
            self._render_window()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self.scroll_rows(3):
            event.stop()
            event.prevent_default()

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self.scroll_rows(-3):
            event.stop()
            event.prevent_default()

    # Keyboard scrolling replaces the touch gesture while the block has focus;
    # at either end the keystroke is left to bubble to the transcript.
    def key_up(self) -> None:
        self.scroll_rows(-1)

    def key_down(self) -> None:
        self.scroll_rows(1)

    def key_pageup(self) -> None:
        self.scroll_screen(-1)

    def key_pagedown(self) -> None:
        self.scroll_screen(1)

    def key_home(self) -> None:
        if self._cursor == (0, 0):
            return  # the transcript takes Home from here
        self._cursor = (0, 0)
        self._follow = False
        self._render_window()

    def key_end(self) -> None:
        if self.at_end:
            return  # the transcript takes End from here
        self._cursor = self._tail_cursor()
        self._follow = True
        self._render_window()

    def _render_window(self) -> None:
        self._max_rows = self._budget()
        rows = self._visible_rows()
        self._window.update(Text("\n").join(rows) if rows else Text(""))
        # The row count changes as the window moves; ask for a layout pass.
        self.refresh(layout=True)
        first, last = self.window_range
        total = len(self._lines)
        counter = (
            f"{first}–{last} of {total} lines"
            if total > 1 and not self._whole_block_visible()
            else ""
        )
        self._counter.update(counter)
        self._up.display = self._cursor != (0, 0)
        self._down.display = not self._last_row_visible()
        # No empty row under a block that fits entirely.
        self._foot.display = self._up.display or self._down.display or bool(counter)

    def _whole_block_visible(self) -> bool:
        return self._cursor == (0, 0) and self._last_row_visible()

    def _budget(self) -> int:
        """Rows one block may use: the pane, or at most a quarter of a screen."""
        try:
            height = self.size.height if self._full_height else self.screen.size.height
        except Exception:  # noqa: BLE001 — not mounted yet
            return 24 if self._full_height else BLOCK_MAX_ROWS
        if self._full_height:
            return max(4, height - 2)  # window rows plus the footer
        return max(3, min(BLOCK_MAX_ROWS, height // 4)) if height > 0 else BLOCK_MAX_ROWS

    def on_mount(self) -> None:
        # The block is filled before it has a screen; lay out the window once
        # the real width and height are known.
        self._render_window()

    def on_resize(self, event: events.Resize) -> None:
        changed = event.size.width != self._width_cache or self._budget() != self._max_rows
        self._width_cache = event.size.width
        if changed:
            self._render_window()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self.scroll_rows(3):
            event.stop()
            event.prevent_default()

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self.scroll_rows(-3):
            event.stop()
            event.prevent_default()

    # Keyboard scrolling replaces the touch gesture while the block has focus;
    # at either end the keystroke is left to bubble to the transcript.
    def key_up(self) -> None:
        self.scroll_rows(-1)

    def key_down(self) -> None:
        self.scroll_rows(1)

    def key_pageup(self) -> None:
        self.scroll_screen(-1)

    def key_pagedown(self) -> None:
        self.scroll_screen(1)

    def key_home(self) -> None:
        if self.scroll_rows(-self._start_row()):
            return

    def key_end(self) -> None:
        self.scroll_rows(self.line_count + self._max_rows)

    def _start_row(self) -> int:
        cursor = self._cursor
        rows = 0
        line, row = 0, 0
        width = self._width()
        while (line, row) < cursor:
            rows += 1
            if row + 1 < len(self._rows(line, width)):
                row += 1
            else:
                line, row = line + 1, 0
        return rows


class ThreadView(Vertical):
    """A read-only pane over one session thread: history plus live frames.

    The body is a pane-sized :class:`BoundedText`, so a long subagent thread
    stays bounded and scrollable by the wheel, tap marks, or the arrow keys
    the same way every other block behaves. The header always shows the
    return affordance and whether the attached (main) thread produced new
    output while this one is being read.
    """

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)
        self._header = Static("", classes="thread-view-header")
        self._body = BoundedText("", classes="thread-view-body", max_rows=0)
        self.thread = ""

    def compose(self):
        yield self._header
        yield self._body

    @property
    def body(self) -> BoundedText:
        return self._body

    def show(self, thread_id: str, summary: str) -> None:
        self.thread = thread_id
        self._body.update("")
        self._refresh_header(summary, main_busy=False)

    def set_main_busy(self, busy: bool, summary: str = "") -> None:
        self._refresh_header(summary, main_busy=busy)

    def _refresh_header(self, summary: str, main_busy: bool) -> None:
        busy = " · main: new output" if main_busy else ""
        self._header.update(
            f"viewing thread {self.thread} · {summary} · read-only{busy} · Ctrl+T / Esc to return"
        )


@dataclass(frozen=True)
class InlineChoice:
    label: str
    kind: str
    payload: dict[str, str]


def message_widget(
    state: TuiState,
    message: TuiMessage,
    *,
    reasoning_expanded: bool = False,
) -> Vertical:
    label = "You" if message.role == "user" else state.agent_name
    return entry_widget_with_renderable(
        message.role,
        f"{message.ts}  {label}",
        render_message(message.content, role=message.role),
        reasoning=render_reasoning(message.reasoning) if message.reasoning else None,
        reasoning_expanded=reasoning_expanded,
    )


def entry_widget_with_renderable(
    kind: str,
    title: str,
    body: Any,
    *,
    reasoning: Text | None = None,
    reasoning_expanded: bool = False,
) -> Vertical:
    children = [Static(render_text(title), classes="meta")]
    if reasoning is not None:
        children.append(reasoning_widget(reasoning, expanded=reasoning_expanded))
    if body:
        children.append(Static(body, classes="body"))
    return Vertical(*children, classes=f"entry {kind}")


def tool_widget(tool: TuiTool, *, details_expanded: bool = False) -> Vertical:
    """Build a single unified tool entry for every tool state.

    The title is TUI-generated from the tool state — the server
    never dictates the presentation.  When a permission check is
    pending the widget shows ``pending approval``; after the
    decision arrives (via ``permission_response_recorded`` or
    ``permission_denied``) it transitions to ``allow (once)`` /
    ``deny``; when the result lands it shows the final status.
    """

    title = _build_title(tool, tool.elapsed(time.monotonic()))
    detail = tool_detail(tool)
    children = [Static(render_text(title), classes="meta")]
    if detail:
        children.append(tool_detail_widget(detail, expanded=details_expanded))
    return Vertical(*children, classes="entry tool")


def reasoning_widget(reasoning: Text, *, expanded: bool = False) -> Collapsible:
    """Render model reasoning as a compact, windowed transcript section."""

    return Collapsible(
        BoundedText(reasoning, classes="reasoning"),
        title="Thinking",
        collapsed=not expanded,
        classes="reasoning-block",
    )


def tool_detail_widget(detail: str, *, expanded: bool = False) -> Collapsible:
    """Render tool arguments and results in a bounded, windowed section."""

    return Collapsible(
        BoundedText(render_text(detail), classes="body"),
        title="Details",
        collapsed=not expanded,
        classes="tool-details",
    )


def _build_title(tool: TuiTool, elapsed: float) -> str:
    args_str = _tool_argument_summary(tool)

    if tool.permission_pending:
        return f"tool  {tool.name}  pending approval  {elapsed:.1f}s…".rstrip()

    if tool.status == "denied":
        return f"tool  {tool.name}  denied  {elapsed:.1f}s".rstrip()

    suffix = ".2f" if tool.finished_at > 0 else ".1f"
    fmt = f"tool  {tool.name}  {args_str}  {tool.status}  {elapsed:{suffix}}s"
    if tool.finished_at <= 0:
        fmt += "…"
    return fmt.rstrip()


def _tool_argument_summary(tool: TuiTool) -> str:
    for key in ("command", "path", "query", "pattern", "objective", "question", "name"):
        if key not in tool.args:
            continue
        value = tool.args[key]
        if not isinstance(value, str):
            value = str(value)
        return shorten(value.replace("\n", " "), width=60, placeholder="...")
    if tool.args_finalized and tool.args_preview:
        return shorten(
            tool.args_preview.replace("\n", " "), width=60, placeholder="..."
        )
    return ""


def tool_detail(tool: TuiTool) -> str:
    parts: list[str] = []
    todo = _todo_projection(tool)
    if todo is not None:
        parts.append("plan:\n" + "\n".join(
            f"  {_todo_marker(str(item.get('status') or ''))} "
            f"{str(item.get('content') or '')}"
            for item in todo
        ))
    if tool.args_finalized and tool.args:
        parts.append(f"args: {format_value(tool.args, indent=2)}")
    elif tool.args_finalized and tool.args_streaming:
        parts.append(f"args: {tool.args_streaming}")
    elif tool.args_finalized and tool.args_preview:
        parts.append(f"args: {tool.args_preview}")
    elif tool.args_streaming:
        parts.append(f"args: {tool.args_streaming}")
    if tool.permission_pending and tool.permission_reason:
        parts.append(tool.permission_reason)
    if tool.result:
        parts.append(f"result: {tool.result}")
    if tool.error:
        parts.append(f"error: {format_value(tool.error, indent=2)}")
    if tool.artifacts:
        parts.append(f"artifacts: {format_value(tool.artifacts, indent=2)}")
    if tool.images:
        parts.append(f"images: {format_value(tool.images, indent=2)}")
    return "\n".join(parts)


def _todo_projection(tool: TuiTool) -> list[dict[str, JsonValue]] | None:
    data = tool.data
    if not isinstance(data, dict) or data.get("kind") != "todo_snapshot":
        return None
    items = data.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        return None
    return items


def _todo_marker(status: str) -> str:
    if status == "completed":
        return "[x]"
    if status == "in_progress":
        return "[>]"
    return "[ ]"


def entry_widget(kind: str, title: str, body: str, *, reasoning: str = "") -> Vertical:
    children = [Static(render_text(title), classes="meta")]
    if reasoning:
        children.append(Static(render_text(reasoning), classes="reasoning"))
    if body:
        children.append(Static(render_text(body), classes="body"))
    return Vertical(*children, classes=f"entry {kind}")


def render_reasoning(content: str) -> Text:
    """Render reasoning content in a visually distinct style.

    The TUI uses this for the ``.reasoning`` Static so the user
    can tell model thinking apart from the final reply. Reasoning
    is dim + italic.
    """
    return Text(content, style="dim italic", no_wrap=False, justify="left")


def render_message(content: str, *, role: str) -> Text | Markdown:
    return _cached_render_message(content, role=role)


def _render_message_uncached(content: str, *, role: str) -> Text | Markdown:
    if role == "assistant":
        markdown = Markdown(content, code_theme="monokai", justify="left")
        plain_tokens = {
            "paragraph_open",
            "paragraph_close",
            "inline",
            "text",
            "softbreak",
        }
        tokens = [
            child
            for token in markdown.parsed
            for child in (token, *(token.children or ()))
        ]
        if any(token.type not in plain_tokens for token in tokens):
            return markdown
    return render_text(content)


def render_text(content: str) -> Text:
    return Text(content, style="default", no_wrap=False, justify="left")


def notice_title(kind: str) -> str:
    return {
        "client_message": "message",
        "permission_denied": "denied",
        "user_input_recorded": "answer",
        "permission_response_recorded": "approval",
        "Not connected": "not connected",
    }.get(kind, kind)


def spinner(index: int) -> str:
    return "|/-\\"[index % 4]
