"""Protocol-only helpers shared by Textual TUI tests and runtime."""

from __future__ import annotations

import asyncio

from rich.text import Text

from xbotv2.tui.client import TuiState, _parse_permission_decision

SubmitRoute = str


def route_submitted_text(
    state: TuiState,
    answers: asyncio.Queue[str],
    permission_decisions: asyncio.Queue[dict[str, str]],
    text: str,
) -> SubmitRoute:
    """Route submitted text to a pending live interaction when present."""
    if state.pending_user_input_payload is not None:
        answers.put_nowait(text)
        return "user_input"
    if state.pending_permission_payload is not None:
        permission_decisions.put_nowait(_parse_permission_decision(text))
        return "permission"
    return "message"


def queue_user_message(
    state: TuiState,
    messages: asyncio.Queue[str],
    text: str,
    *,
    append_now: bool = False,
) -> None:
    """Queue a normal user message; append to transcript only when consumed."""
    messages.put_nowait(text)
    if append_now:
        state.append_message("user", text)


def render_transcript_entry(state: TuiState, entry: object) -> Text | None:
    """Render one transcript entry without Textual app/runtime imports."""
    kind = str(getattr(entry, "kind", ""))
    key = str(getattr(entry, "key", ""))
    if kind == "message":
        try:
            message = state.messages[int(key)]
        except (ValueError, IndexError):
            return None
        label = "You" if message.role == "user" else state.agent_name
        color = "cyan" if message.role == "user" else "green"
        text = Text()
        text.append(f"{label}\n", style=f"bold {color}")
        if message.reasoning:
            text.append("(thinking) ", style="dim italic")
            text.append(message.reasoning, style="dim italic")
            text.append("\n")
        text.append(message.content)
        return text
    if kind == "tool":
        tool = state.tools.get(key)
        if tool is None:
            return None
        text = Text()
        text.append("Tool ", style="bold yellow")
        text.append(tool.name, style="yellow")
        text.append(f" [{tool.status}]\n", style="dim")
        if tool.args_finalized and tool.args_preview:
            text.append(f"args: {tool.args_preview}\n", style="dim")
        elif tool.args_streaming:
            text.append(f"args: {tool.args_streaming}…\n", style="dim")
        if tool.permission_pending:
            text.append("  waiting for approval…\n", style="dim italic")
        if tool.summary:
            text.append(f"result: {tool.summary}")
        return text
    if kind == "notice":
        try:
            notice = state.notices[int(key)]
        except (ValueError, IndexError):
            return None
        text = Text()
        text.append(f"{notice.kind}\n", style="bold magenta")
        text.append(notice.text)
        return text
    if kind == "error":
        try:
            error = state.errors[int(key)]
        except (ValueError, IndexError):
            return None
        text = Text()
        text.append("Error\n", style="bold red")
        text.append(error, style="red")
        return text
    return None
