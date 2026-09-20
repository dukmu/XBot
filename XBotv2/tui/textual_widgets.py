"""Textual widgets and render helpers for the protocol TUI."""

from __future__ import annotations

import asyncio
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

from XBotv2.tui.client import (
    TuiMessage,
    TuiNotice,
    TuiState,
    TuiJob,
    TuiTool,
    TuiTranscriptEntry,
    format_value,
)

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
    session_title: str = "",
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

    # Detailed token I/O stays on wide lines, but yields to a readable
    # session title plus agent/model identity on ordinary terminals.
    detailed_tokens_allowed = width >= 96
    if (
        detailed_tokens_allowed
        and width < 160
        and session_title
        and session_title != session_id
    ):
        detailed_tokens_allowed = False
    if detailed_tokens_allowed:
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
    # The readable session title is the primary human identity on the Web
    # header, so surface it before secondary runtime details on narrow but
    # usable terminals. Wide lines keep the existing combined session row.
    if (
        width < 160
        and width >= 60
        and session_title
        and session_title != session_id
    ):
        optional.append((f"title:{session_title[:32]}", "dim"))
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
    if width >= 160:
        session = (
            session_title
            if thread_id == "agent"
            else f"{session_title}/{thread_id}"
        )
        optional.append((f"session:{session[:44]}", "dim"))
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


def jobs_renderable(tasks: list[TuiJob], *, width: int) -> Text:
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
            12, width - len(task.job_id) - len(marker) - len(kind) - 15
        )
        command = shorten(task.command, width=summary_width, placeholder="...")
        if text.plain:
            text.append("\n")
        text.append(f"{marker:>7}  ", style=style)
        text.append(f"{task.job_id}  ", style="cyan")
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


class SubagentJobWidget(Collapsible):
    """One expandable task with the full command/details behind a bounded window."""

    def __init__(
        self,
        task: TuiJob,
        *,
        width: int,
        collapsed: bool = True,
    ) -> None:
        self.job_id = task.job_id
        self._latest_task = task
        self._width = width
        super().__init__(
            BoundedText(job_detail_text(task), classes="job-detail"),
            title=_job_title(task, width=width),
            collapsed=collapsed,
            classes="subagent-job",
        )

    def update_job(self, task: TuiJob, *, width: int) -> None:
        """Update one existing task row in place, preserving expansion."""
        self._latest_task = task
        self._width = width
        self.title = _job_title(task, width=width)
        if not self._update_detail():
            # The row may have been mounted on this tick but not composed
            # yet; apply the latest task after the refresh pass.
            self.call_after_refresh(self._update_detail)

    def _update_detail(self) -> bool:
        try:
            detail = self.query_one(".job-detail", BoundedText)
        except Exception:  # noqa: BLE001 — child composition may lag mount
            return False
        detail.update(job_detail_text(self._latest_task))
        return True


def job_detail_text(task: TuiJob) -> str:
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


class JobListWidget(VerticalScroll):
    """Scrollable task list with nested subagent details."""

    can_focus = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._widgets: dict[str, SubagentJobWidget] = {}

    def update_jobs(self, tasks: list[TuiJob], *, width: int) -> None:
        """Reconcile task widgets in place instead of rebuilding the list.

        Rebuilding on every tick made expanded task details flicker and
        collapse. Existing widgets keep their identity and expansion state;
        running titles/details are refreshed in place.
        """
        desired = {task.job_id: task for task in tasks}
        for job_id, widget in list(self._widgets.items()):
            if job_id in desired:
                continue
            self._widgets.pop(job_id, None)
            widget.remove()

        ordered: list[SubagentJobWidget] = []
        for task in tasks:
            widget = self._widgets.get(task.job_id)
            if widget is None:
                widget = SubagentJobWidget(task, width=width)
                self._widgets[task.job_id] = widget
                self.mount(widget)
            else:
                widget.update_job(task, width=width)
            ordered.append(widget)

        for index, widget in enumerate(ordered):
            if index < len(self.children) and self.children[index] is widget:
                continue
            self.move_child(widget, before=index)


def _job_title(task: TuiJob, *, width: int) -> str:
    marker = {
        "pending": "-",
        "running": "running",
        "completed": "done",
        "failed": "failed",
        "stopped": "stopped",
    }.get(task.status, task.status)
    agent = task.agent or task.command.partition(":")[0] or "subagent"
    available = max(12, width - len(task.job_id) - len(marker) - len(agent) - 8)
    prompt = task.command.partition(":")[2].strip() or task.command
    return (
        f"{marker}  {task.job_id}  {agent}  "
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

    class TopReached(Message):
        """The window reached the first row; a viewer may lazy-load older text."""

    def prepend(self, text: str) -> None:
        """Insert older text before the current content, keeping the view put."""
        if not text:
            return
        added = text.split("\n")
        line, row = self._cursor
        self._lines = added + (self._lines if self._plain else [""])
        self._plain = text + self._plain
        self._cursor = (line + len(added), row)
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
        elif self._cursor == (0, 0):
            self.post_message(self.TopReached())

    # Keyboard scrolling replaces the touch gesture while the block has focus;
    # at either end the keystroke is left to bubble to the transcript.
    def key_up(self) -> None:
        if not self.scroll_rows(-1) and self._cursor == (0, 0):
            self.post_message(self.TopReached())

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

class ThreadView(Vertical):
    """A read-only transcript over one session thread.

    It reuses the same event-driven :class:`TranscriptSurface` and
    :class:`TranscriptScroll` contract as the main transcript. The only
    difference is the container id and the caller-provided event source.
    """

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)
        self._header = Static("", classes="thread-view-header")
        self._body = TranscriptScroll(
            id="thread_transcript",
            classes="thread-view-body",
        )
        self.thread = ""
        self._surface: TranscriptSurface | None = None
        self._items: list[dict[str, Any]] = []
        self._agent_name = ""
        self._follow = True

    def compose(self):
        # The transcript body is mounted only when the read-only view opens;
        # keeping the hidden widget out of the initial DOM avoids competing
        # with the main transcript's focus lifecycle on startup.
        yield self._header

    async def _mount_body(self) -> None:
        if self._body.parent is None:
            await self.mount(self._body)

    @property
    def body(self) -> TranscriptScroll:
        return self._body

    def show(self, thread_id: str, summary: str) -> None:
        self.thread = thread_id
        self.reset()
        self._refresh_header(summary, main_busy=False)

    def reset(self) -> None:
        self._items = []
        self._follow = True
        if self._surface is not None:
            self._surface.set_state(TuiState())

    @staticmethod
    def _build_state(
        items: list[dict[str, Any]],
        agent_name: str,
    ) -> TuiState:
        state = TuiState(agent_name=agent_name or "subagent")
        history: list[dict[str, Any]] = []
        compactions: list[tuple[str, str]] = []
        for item in items:
            kind = str(item.get("kind") or "")
            if kind == "message" and isinstance(item.get("message"), dict):
                message = dict(item["message"])
                message["message_id"] = str(
                    item.get("message_id") or message.get("message_id") or ""
                )
                history.append(message)
            elif kind == "surface_replace":
                summary = str(item.get("summary") or "")
                if summary:
                    compactions.append((str(item.get("operation") or ""), summary))
        if history:
            state.restore_history(history)
        for operation, summary in compactions:
            label = "Conversation compacted"
            if operation.startswith("compact:"):
                label = f"Conversation compacted ({operation[8:]})"
            state.append_notice(
                "compact",
                label,
                payload={"summary": summary},
            )
        return state

    async def load(
        self,
        items: list[dict[str, Any]],
        *,
        agent_name: str = "",
    ) -> None:
        await self._mount_body()
        self._items = list(items)
        self._agent_name = agent_name
        state = self._build_state(self._items, agent_name)
        self._surface = TranscriptSurface(
            state,
            self._body,
            max_mounted_entries=200,
        )
        await self._surface.replace(state, mount_all=True)
        self._follow = True
        self._body.call_after_refresh(self._body.scroll_end, animate=False)

    async def prepend_items(
        self,
        records: list[dict[str, Any]],
        *,
        agent_name: str = "",
    ) -> None:
        """Prepend one older trajectory page while keeping the old top item."""
        if not records:
            return
        old_items = self._items
        older_state = self._build_state(records, agent_name)
        self._items = [*records, *old_items]
        self._agent_name = agent_name or self._agent_name
        state = self._build_state(self._items, self._agent_name)
        anchor_index = len(older_state.transcript)
        anchor_entry = (
            state.transcript[anchor_index]
            if anchor_index < len(state.transcript)
            else (state.transcript[0] if state.transcript else None)
        )
        self._surface = TranscriptSurface(
            state,
            self._body,
            max_mounted_entries=200,
        )
        await self._surface.replace(state, mount_all=True)
        self._follow = False
        if anchor_entry is not None:
            anchor_widget = self._surface.widget_for_entry(anchor_entry)
            if anchor_widget is not None:
                self._body.call_after_refresh(
                    lambda w=anchor_widget: self._body.scroll_to_widget(
                        w,
                        top=True,
                        animate=False,
                    )
                )

    async def catch_up(self) -> None:
        if self._surface is None:
            return
        self._follow = True
        await self._surface.catch_up()

    async def apply_event(self, event: dict[str, Any]) -> None:
        if self._surface is None:
            return
        if str(event.get("type") or "") == "end":
            return
        self._follow = self._body.is_vertical_scroll_end
        await self._surface.apply_event(event, follow=self._follow)

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


def compact_widget(*, title: str, summary: str) -> Collapsible:
    """A collapsed context entry whose body holds the full compaction summary."""

    return Collapsible(
        BoundedText(summary, classes="compact-summary"),
        title=title,
        collapsed=True,
        classes="compact-block",
    )


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
        parts.append("tasks:\n" + "\n".join(
            f"  {_todo_marker(str(item.get('status') or ''))} "
            f"#{str(item.get('id') or '')} {str(item.get('subject') or '')}"
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
    items = data.get("jobs")
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


class TranscriptSurface:
    """Event-driven transcript renderer shared by the main and thread views.

    The surface owns only presentation state: the :class:`TuiState` is fed by
    the caller, while this class materializes transcript entries with the same
    semantic widgets and keeps the mounted window bounded. Both the main
    client surface and the read-only thread view use this class, so message,
    reasoning, tool, and notice rendering has one implementation.
    """

    def __init__(
        self,
        state: TuiState,
        container: VerticalScroll,
        *,
        notice_widget_factory: Any | None = None,
        tool_extra: Any | None = None,
        reasoning_expanded: Any | None = None,
        details_expanded: Any | None = None,
        max_mounted_entries: int = 100,
        max_message_widgets: int = 200,
        max_tool_widgets: int = 100,
    ) -> None:
        self.state = state
        self.container = container
        self.notice_widget_factory = notice_widget_factory
        self.tool_extra = tool_extra
        self.reasoning_expanded = reasoning_expanded or (lambda: False)
        self.details_expanded = details_expanded or (lambda: False)
        self.max_mounted_entries = max_mounted_entries
        self.max_message_widgets = max_message_widgets
        self.max_tool_widgets = max_tool_widgets
        self.window_start = 0
        self.window_end = 0
        self.mounted_entry_widgets: list[Any] = []
        # Transcript index of each mounted widget, in the same order.  The
        # window bounds are derived from these: deriving them from the widget
        # *count* assumed every entry renders a widget, which is false for
        # entries whose payload is gone, and the resulting drift re-mounted
        # widgets the reader could already see.
        self._mounted_entry_indices: list[int] = []
        self.message_widgets: dict[int, Vertical] = {}
        self.tool_widgets: dict[str, Vertical] = {}
        self.render_lock = asyncio.Lock()
        # Evictions observed from the state so far.  The state trims its
        # retained window from the front while this surface holds absolute
        # positions in that same transcript, so both the window and the
        # index-keyed widget cache are shifted by the delta.
        self._seen_transcript_evictions = self.state.evicted_transcript
        self._seen_tail_evictions = self.state.evicted_transcript_tail
        self._seen_insertions = self.state.inserted_transcript
        self._seen_message_insertions = self.state.inserted_messages
        self._seen_message_evictions = self.state.evicted_messages
        self._window_invalidated = False

    async def settle_window(self) -> None:
        """Bring the window back in line with the state after a mutation."""
        self.reconcile_evictions()
        if self._window_invalidated:
            await self._remount_tail()
            return
        await self.trim_mounted_suffix()

    def reconcile_evictions(self) -> None:
        """Re-anchor window and caches after the state evicted old payloads.

        Evictions only ever remove the oldest entries, so a window that starts
        at or past the number removed still points at the entries it was
        mounted for and is shifted in place.  A window that overlapped the
        removed region holds content the client no longer has and is re-mounted
        from the retained tail.  Index-keyed widget caches are shifted by the
        state's own renumbering of retained payloads.
        """
        inserted = self.state.inserted_transcript - self._seen_insertions
        if inserted > 0:
            # An older page was prepended: the reader's entries moved down.
            self._seen_insertions = self.state.inserted_transcript
            self.window_start += inserted
            self.window_end += inserted
        added = self.state.inserted_messages - self._seen_message_insertions
        if added > 0:
            # Message indices were renumbered by the same amount; the prepended
            # indices have no widget yet and must not reuse a shifted one.
            self._seen_message_insertions = self.state.inserted_messages
            self.message_widgets = {
                index + added: widget for index, widget in self.message_widgets.items()
            }
        tail = self.state.evicted_transcript_tail - self._seen_tail_evictions
        if tail > 0:
            # The newest entries were dropped while the reader was inside
            # history: the window simply ends earlier.
            self._seen_tail_evictions = self.state.evicted_transcript_tail
            self.window_end = min(self.window_end, len(self.state.transcript))
        shift = self.state.evicted_transcript - self._seen_transcript_evictions
        if shift > 0:
            self._seen_transcript_evictions = self.state.evicted_transcript
            if self.window_start >= shift:
                self.window_start -= shift
                self.window_end -= shift
            else:
                self._window_invalidated = True
        message_shift = self.state.evicted_messages - self._seen_message_evictions
        if message_shift > 0:
            self._seen_message_evictions = self.state.evicted_messages
            self.message_widgets = {
                index - message_shift: widget
                for index, widget in self.message_widgets.items()
                if index >= message_shift
            }

    async def _remount_tail(self) -> None:
        """Re-mount the retained tail after the window's entries were evicted."""
        self._window_invalidated = False
        await self.clear()
        total = len(self.state.transcript)
        self.window_start = max(0, total - self.max_mounted_entries)
        self.window_end = self.window_start
        await self.mount_entries(self.window_start, total)
        self.window_end = total

    def set_state(self, state: TuiState) -> None:
        self.state = state
        self.window_start = 0
        self.window_end = 0
        self.mounted_entry_widgets = []
        self._mounted_entry_indices = []
        self.message_widgets.clear()
        self.tool_widgets.clear()
        self._seen_transcript_evictions = state.evicted_transcript
        self._seen_tail_evictions = state.evicted_transcript_tail
        self._seen_insertions = state.inserted_transcript
        self._seen_message_insertions = state.inserted_messages
        self._seen_message_evictions = state.evicted_messages
        self._window_invalidated = False

    async def clear(self) -> None:
        await self.container.remove_children()
        self.window_start = 0
        self.window_end = 0
        self.mounted_entry_widgets = []
        self._mounted_entry_indices = []
        self.message_widgets.clear()
        self.tool_widgets.clear()
        self._seen_transcript_evictions = self.state.evicted_transcript
        self._seen_tail_evictions = self.state.evicted_transcript_tail
        self._seen_insertions = self.state.inserted_transcript
        self._seen_message_insertions = self.state.inserted_messages
        self._seen_message_evictions = self.state.evicted_messages
        self._window_invalidated = False

    async def rebuild_from_state(self) -> None:
        await self.clear()
        await self.mount_entries(0, len(self.state.transcript))
        self.window_end = len(self.state.transcript)

    async def replace(
        self,
        state: TuiState,
        *,
        mount_all: bool = False,
    ) -> None:
        self.set_state(state)
        await self.clear()
        if mount_all and state.transcript:
            await self.mount_entries(0, len(state.transcript))
            self.window_end = len(state.transcript)

    def _schedule_refresh(self, callback: Any) -> None:
        app = getattr(self.container, "app", None)
        schedule = getattr(app, "call_after_refresh", None)
        if callable(schedule):
            schedule(callback)
        else:
            self.container.call_after_refresh(callback)

    async def sync(self, *, follow: bool | None = None) -> bool:
        """Mount new transcript entries following the live tail."""
        async with self.render_lock:
            await self.settle_window()
            end = len(self.state.transcript)
            if follow is None:
                follow = self.container.is_vertical_scroll_end
            if end <= self.window_end:
                await self.drop_leading_excess(follow)
                return False
            if not follow:
                return False
            # Mount at most one window's worth per sync.  Entries between the
            # mounted window and the tail are skipped rather than materialized
            # and immediately trimmed; they stay in the state window and are
            # mounted on demand if the reader scrolls back.  A burst of events
            # therefore costs one window, not one widget per event.
            start = max(self.window_end, end - self.max_mounted_entries)
            await self.mount_entries(start, end)
            self.window_end = end
            await self.trim_mounted_prefix()
            if follow:
                self._schedule_refresh(
                    lambda: self.container.scroll_end(animate=False)
                )
            return True

    async def catch_up(self) -> bool:
        """Mount entries that accumulated while the reader was scrolled up."""
        return await self.sync(follow=True)

    async def mount_entries(
        self,
        start: int,
        end: int,
        *,
        prepend: bool = False,
    ) -> list[Any]:
        await self.settle_window()
        if self._window_invalidated:
            await self._remount_tail()
            return list(self.mounted_entry_widgets)
        # Only widgets this call actually mounts are reported and tracked.  A
        # cached widget whose entry is still on screen is already accounted for,
        # and counting it as inserted made the caller compensate the reader's
        # scroll position for height that never appeared.
        mounted: list[Any] = []
        indices: list[int] = []
        reference = (
            self.container.children[0]
            if prepend and self.container.children
            else None
        )
        for index, entry in enumerate(self.state.transcript[start:end], start=start):
            widget = self.widget_for_entry(entry)
            if widget is None or widget.parent is self.container:
                continue
            if widget.parent is not None:
                try:
                    await widget.remove()
                except Exception:  # noqa: BLE001
                    pass
            if reference is not None:
                await self.container.mount(widget, before=reference)
            else:
                await self.container.mount(widget)
            mounted.append(widget)
            indices.append(index)
        if prepend:
            self.mounted_entry_widgets[0:0] = mounted
            self._mounted_entry_indices[0:0] = indices
        else:
            self.mounted_entry_widgets.extend(mounted)
            self._mounted_entry_indices.extend(indices)
        if indices:
            # The window starts at the oldest entry that is actually mounted.
            self.window_start = self._mounted_entry_indices[0]
            if not prepend:
                self.window_end = max(self.window_end, self._mounted_entry_indices[-1] + 1)
        return mounted

    async def trim_mounted_suffix(self) -> int:
        """Drop mounted widgets for entries the state no longer retains."""
        total = len(self.state.transcript)
        keep = len(self._mounted_entry_indices)
        for position, index in enumerate(self._mounted_entry_indices):
            if index >= total:
                keep = position
                break
        excess = len(self.mounted_entry_widgets) - keep
        if excess <= 0:
            return 0
        removed = self.mounted_entry_widgets[keep:]
        self.mounted_entry_widgets = self.mounted_entry_widgets[:keep]
        self._mounted_entry_indices = self._mounted_entry_indices[:keep]
        for widget in removed:
            if widget.parent is self.container:
                await widget.remove()
        if self._mounted_entry_indices:
            self.window_end = min(self.window_end, self._mounted_entry_indices[-1] + 1)
        else:
            self.window_end = self.window_start
        return excess

    async def trim_mounted_prefix(self) -> int:
        """Keep the mounted window at the cap, dropping the oldest widgets.

        The window is the contiguous range the surviving widgets cover, so it
        is derived from the widget count rather than adjusted by a delta.
        """
        excess = len(self.mounted_entry_widgets) - self.max_mounted_entries
        if excess > 0:
            removed = self.mounted_entry_widgets[:excess]
            self.mounted_entry_widgets = self.mounted_entry_widgets[excess:]
            self._mounted_entry_indices = self._mounted_entry_indices[excess:]
            for widget in removed:
                if widget.parent is self.container:
                    await widget.remove()
        if self._mounted_entry_indices:
            self.window_start = self._mounted_entry_indices[0]
        return max(0, excess)

    async def drop_leading_excess(self, follow: bool) -> int:
        if not follow:
            return 0
        await self.settle_window()
        excess = self.window_end - self.window_start - self.max_mounted_entries
        if excess <= 0:
            return 0
        removed = self.mounted_entry_widgets[:excess]
        self.mounted_entry_widgets = self.mounted_entry_widgets[excess:]
        self._mounted_entry_indices = self._mounted_entry_indices[excess:]
        for widget in removed:
            if widget.parent is self.container:
                await widget.remove()
        self.window_start = (
            self._mounted_entry_indices[0]
            if self._mounted_entry_indices
            else self.window_end
        )
        return len(removed)

    async def drop_trailing_excess(self) -> int:
        await self.settle_window()
        excess = self.window_end - self.window_start - self.max_mounted_entries
        if excess <= 0:
            return 0
        removed = self.mounted_entry_widgets[-excess:]
        self.mounted_entry_widgets = self.mounted_entry_widgets[:-excess]
        self._mounted_entry_indices = self._mounted_entry_indices[:-excess]
        for widget in removed:
            if widget.parent is self.container:
                await widget.remove()
        self.window_end = (
            self._mounted_entry_indices[-1] + 1
            if self._mounted_entry_indices
            else self.window_start
        )
        return len(removed)

    def widget_for_entry(self, entry: TuiTranscriptEntry) -> Vertical | Static | None:
        kind = str(getattr(entry, "kind", ""))
        key = str(getattr(entry, "key", ""))
        if kind == "message":
            try:
                index = int(key)
                message = self.state.messages[index]
            except (ValueError, IndexError):
                return None
            existing = self.message_widgets.get(index)
            if existing is not None:
                return existing
            widget = message_widget(
                self.state,
                message,
                reasoning_expanded=bool(self.reasoning_expanded()),
            )
            self.message_widgets[index] = widget
            self.trim_message_widgets()
            return widget
        if kind == "tool":
            tool = self.state.tools.get(key)
            if tool is None:
                return None
            widget_id = tool.tool_call_id
            existing = self.tool_widgets.get(widget_id)
            if existing is not None:
                try:
                    self.refresh_tool_widget_sync(widget_id)
                except Exception:  # noqa: BLE001
                    pass
                return existing
            widget = tool_widget(
                tool,
                details_expanded=bool(self.details_expanded()),
            )
            self.tool_widgets[widget_id] = widget
            self.trim_tool_widgets()
            return widget
        if kind == "notice":
            try:
                notice = self.state.notices[int(key)]
            except (ValueError, IndexError):
                return None
            if self.notice_widget_factory is not None:
                return self.notice_widget_factory(notice, key)
            return self._default_notice_widget(notice)
        if kind == "error":
            try:
                error = self.state.errors[int(key)]
            except (ValueError, IndexError):
                return None
            return entry_widget("error", "Error", error)
        return None

    @staticmethod
    def _default_notice_widget(notice: TuiNotice) -> Vertical:
        if notice.kind == "compact":
            summary = str(notice.payload.get("summary") or "")
            if summary:
                return compact_widget(
                    title=f"{notice.ts}  {notice.text}",
                    summary=summary,
                )
        return entry_widget(
            "notice",
            f"{notice.ts}  {notice_title(notice.kind)}",
            notice.text,
        )

    def trim_message_widgets(self) -> None:
        while len(self.message_widgets) > self.max_message_widgets:
            oldest = next(iter(self.message_widgets))
            self.message_widgets.pop(oldest, None)

    def trim_tool_widgets(self) -> None:
        while len(self.tool_widgets) > self.max_tool_widgets:
            oldest = next(iter(self.tool_widgets))
            self.tool_widgets.pop(oldest, None)

    def refresh_tool_widget_sync(self, tool_call_id: str) -> None:
        tool = self.state.tools.get(tool_call_id)
        widget = self.tool_widgets.get(tool_call_id)
        if tool is None or widget is None:
            return
        title = _build_title(tool, tool.elapsed(time.monotonic()))
        meta = _query_child(widget, ".meta")
        if meta is not None:
            meta.update(title)
        detail = tool_detail(tool)
        body = _query_child(widget, ".body")
        if body is not None:
            body.update(render_text(detail))
        elif detail:
            widget.mount(
                tool_detail_widget(
                    detail,
                    expanded=bool(self.details_expanded()),
                )
            )

    async def refresh_changed_tool_widgets(
        self,
        tool_ids: set[str] | None = None,
    ) -> None:
        for old_id, new_id in self.state._tool_id_renames.items():
            widget = self.tool_widgets.pop(old_id, None)
            if widget is None:
                continue
            existing = self.tool_widgets.get(new_id)
            if existing is not None and existing is not widget:
                # The final tool widget was already mounted from history or
                # a previous frame. Drop the provisional duplicate instead of
                # replacing the durable widget with the streaming one.
                if widget.parent is self.container:
                    await widget.remove()
                self.mounted_entry_widgets = [
                    item for item in self.mounted_entry_widgets if item is not widget
                ]
                continue
            self.tool_widgets[new_id] = widget
        changed_ids = (
            tool_ids if tool_ids is not None else self.state._changed_tool_ids
        )
        for tool_call_id in list(changed_ids):
            await self.refresh_tool_widget(tool_call_id)

    async def refresh_streaming_assistant_widget(
        self,
        *,
        follow: bool | None = None,
    ) -> None:
        index = self.state._streaming_assistant_index
        if index is None and self.state.messages:
            index = len(self.state.messages) - 1
        if index is None:
            return
        try:
            message = self.state.messages[index]
        except IndexError:
            return
        widget = self.message_widgets.get(index)
        if widget is None:
            await self.sync(follow=follow)
            widget = self.message_widgets.get(index)
        if widget is None:
            return
        follow_output = (
            self.container.is_vertical_scroll_end if follow is None else follow
        )
        await self.apply_streaming_message_widget(widget, message)
        if follow_output:
            self.container.scroll_end(animate=False)

    async def apply_streaming_message_widget(
        self,
        widget: Any,
        message: TuiMessage,
    ) -> None:
        reasoning = _query_child(widget, ".reasoning")
        if message.reasoning:
            if reasoning is not None:
                reasoning.update(render_reasoning(message.reasoning))
            else:
                block = reasoning_widget(
                    render_reasoning(message.reasoning),
                    expanded=bool(self.reasoning_expanded()),
                )
                body = _query_child(widget, ".body")
                await widget.mount(block, before=body)
        body = _query_child(widget, ".body")
        if body is not None:
            body.update(render_message(message.content, role=message.role))
        elif message.content:
            await widget.mount(
                Static(
                    render_message(message.content, role=message.role),
                    classes="body",
                )
            )

    async def refresh_tool_widget(self, tool_call_id: str) -> None:
        if not tool_call_id:
            return
        tool = self.state.tools.get(tool_call_id)
        widget = self.tool_widgets.get(tool_call_id)
        if tool is None or widget is None:
            return
        title = _build_title(tool, tool.elapsed(time.monotonic()))
        meta = _query_child(widget, ".meta")
        if meta is None:
            return
        meta.update(title)
        detail = tool_detail(tool)
        body = _query_child(widget, ".body")
        if body is not None:
            body.update(render_text(detail))
        elif detail:
            await widget.mount(
                tool_detail_widget(
                    detail,
                    expanded=bool(self.details_expanded()),
                )
            )
        if self.tool_extra is not None:
            await self.tool_extra(widget, tool)

    async def apply_event(
        self,
        event: dict[str, Any],
        *,
        follow: bool | None = None,
    ) -> None:
        """Apply one normalized event to this surface's state and sync."""
        event_type = str(event.get("type") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event_type == "message" and str(data.get("role") or "user") == "user":
            self.state.append_message(
                "user",
                str(data.get("content") or ""),
                message_id=str(data.get("id") or ""),
            )
        else:
            self.state.apply_event(event)
        if event_type == "history_updated":
            await self.rebuild_from_state()
            return
        await self.sync(follow=follow)
        if event_type in {"assistant_message", "assistant_message_delta"}:
            await self.refresh_streaming_assistant_widget(follow=follow)
        if self.state._changed_tool_ids:
            await self.refresh_changed_tool_widgets()


def _query_child(widget: Any, selector: str) -> Any | None:
    try:
        return widget.query(selector).first()
    except Exception:  # noqa: BLE001 — child may not exist yet
        return None


def spinner(index: int) -> str:
    return "|/-\\"[index % 4]
