"""Textual widgets and render helpers for the protocol TUI."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.events import Key
from textual.widgets import Static, TextArea
from textwrap import shorten

from xbotv2.tui.client import TuiMessage, TuiState, TuiTool


_STATUS_BADGE_STYLE: dict[str, str] = {
    "Ready": "green",
    "Running": "yellow",
    "Connecting": "yellow",
    "Waiting for user": "cyan",
    "Approval required": "magenta",
    "Permission denied": "red",
    "Error": "red",
    "Shutdown": "dim",
}


def status_renderable(
    *,
    status: str,
    session_id: str,
    thread_id: str,
    agent_name: str,
    activity: str,
    queue_depth: int,
    usage: dict[str, int],
) -> Text:
    """Build the status bar as styled text without markup parsing."""

    style = _STATUS_BADGE_STYLE.get(status, "white")
    text = Text()
    text.append("XBotv2", style="bold")
    text.append("  ")
    text.append(status, style=style)
    text.append("  ")
    text.append(f"{session_id}/{thread_id}")
    text.append("  ")
    text.append(f"agent:{agent_name}")
    text.append("  ")
    text.append(activity)
    text.append("  ")
    text.append(f"queued:{queue_depth}")
    text.append("  ")
    text.append(
        f"usage req:{usage['requests']} "
        f"in:{usage['input_tokens']} "
        f"out:{usage['output_tokens']} "
        f"total:{usage['total_tokens']}"
    )
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
        await super()._on_key(event)


class TranscriptScroll(VerticalScroll):
    """Mouse-scrollable transcript that never takes keyboard focus."""

    can_focus = False


@dataclass(frozen=True)
class InlineChoice:
    label: str
    kind: str
    payload: dict[str, str]


def message_widget(state: TuiState, message: TuiMessage) -> Vertical:
    label = "You" if message.role == "user" else state.agent_name
    return entry_widget_with_renderable(
        message.role,
        f"{message.ts}  {label}",
        render_text(message.content),
        reasoning=render_reasoning(message.reasoning) if message.reasoning else None,
    )


def entry_widget_with_renderable(
    kind: str,
    title: str,
    body: Text | str,
    *,
    reasoning: Text | None = None,
) -> Vertical:
    children = [Static(render_text(title), classes="meta")]
    if reasoning is not None:
        children.append(Static(reasoning, classes="reasoning"))
    if body:
        children.append(Static(body, classes="body"))
    return Vertical(*children, classes=f"entry {kind}")


def tool_widget(tool: TuiTool) -> Vertical:
    """Build a single unified tool entry for every tool state.

    The title is TUI-generated from the tool state — the server
    never dictates the presentation.  When a permission check is
    pending the widget shows ``pending approval``; after the
    decision arrives (via ``permission_response_recorded`` or
    ``permission_denied``) it transitions to ``allow (once)`` /
    ``deny``; when the result lands it shows the final status.
    """

    elapsed = tool.elapsed(time.monotonic())
    if tool.permission_pending:
        title = _build_title(tool, elapsed)
    elif tool.finished_at > 0:
        title = _build_title(tool, elapsed)
    else:
        title = _build_title(tool, elapsed)
    return entry_widget("tool", title, tool_detail(tool))


def _build_title(tool: TuiTool, elapsed: float) -> str:
    args_str = tool.args_preview if tool.args_finalized else ""

    if tool.permission_pending:
        return f"tool  {tool.name}  pending approval  {elapsed:.1f}s…".rstrip()

    if tool.status == "denied":
        return f"tool  {tool.name}  denied  {elapsed:.1f}s".rstrip()

    suffix = ".2f" if tool.finished_at > 0 else ".1f"
    fmt = f"tool  {tool.name}  {args_str}  {tool.status}  {elapsed:{suffix}}s"
    if tool.finished_at <= 0:
        fmt += "…"
    return fmt.rstrip()


def tool_detail(tool: TuiTool) -> str:
    parts: list[str] = []
    if tool.args_finalized and tool.args_preview:
        parts.append(f"args: {tool.args_preview}")
    elif tool.args_streaming:
        parts.append(f"args: {shorten(tool.args_streaming, width=160, placeholder='…')}")
    if tool.permission_pending and tool.permission_reason:
        parts.append(tool.permission_reason)
    if tool.summary:
        parts.append(f"result: {tool.summary}")
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
