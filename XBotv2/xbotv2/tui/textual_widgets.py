"""Textual widgets and render helpers for the protocol TUI."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from textwrap import shorten
from typing import Any

from rich.markdown import Markdown
from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.events import Key
from textual.widgets import Collapsible, Static, TextArea

from xbotv2.tui.client import TuiMessage, TuiState, TuiTask, TuiTool, format_value


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
        detailed_tokens = (
            f"tokens:{_compact_count(total)} "
            f"({_compact_count(usage['input_tokens'])} in / "
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
    """One expandable subagent task with a fixed-height scrollable body."""

    def __init__(
        self,
        task: TuiTask,
        *,
        width: int,
        collapsed: bool = True,
    ) -> None:
        self.task_id = task.task_id
        output = task.error or task.output or "Waiting for subagent output..."
        details = [f"thread: {task.thread_id or '-'}"]
        total = int(task.usage.get("total_tokens") or 0)
        if total:
            details.append(f"tokens: {_compact_count(total)}")
        details.extend(("", output))
        body = VerticalScroll(
            Static("\n".join(details), markup=False),
            classes="subagent-output",
        )
        super().__init__(
            body,
            title=_task_title(task, width=width),
            collapsed=collapsed,
            classes="subagent-task",
        )


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
        widgets: list[Static | SubagentTaskWidget] = []
        for task in tasks:
            if task.kind == "agent":
                widgets.append(
                    SubagentTaskWidget(
                        task,
                        width=width,
                        collapsed=task.task_id not in expanded,
                    )
                )
            else:
                widgets.append(
                    Static(
                        tasks_renderable([task], width=width),
                        classes="task-row",
                    )
                )
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
    """Mouse-scrollable transcript that never takes keyboard focus."""

    can_focus = False


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
    """Render model reasoning as a compact, collapsed transcript section."""

    return Collapsible(
        Static(reasoning, classes="reasoning"),
        title="Thinking",
        collapsed=not expanded,
        classes="reasoning-block",
    )


def tool_detail_widget(detail: str, *, expanded: bool = False) -> Collapsible:
    """Render tool arguments and results without hiding the live summary."""

    return Collapsible(
        Static(render_text(detail), classes="body"),
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
    if tool.data is not None:
        parts.append(f"data: {format_value(tool.data, indent=2)}")
    if tool.error:
        parts.append(f"error: {format_value(tool.error, indent=2)}")
    if tool.artifacts:
        parts.append(f"artifacts: {format_value(tool.artifacts, indent=2)}")
    return "\n".join(parts)


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
