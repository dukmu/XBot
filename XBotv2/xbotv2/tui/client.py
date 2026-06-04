"""Protocol-driven curses TUI client.

The TUI layer consumes protocol events only. It must not import runtime,
bootstrap, LangChain, or LangGraph modules.
"""

from __future__ import annotations

import asyncio
import curses
import json
import queue
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import shorten
from typing import Any

from xbotv2.protocol.frames import ProtocolFrame
from xbotv2.tui.terminal import TerminalSession

_TERMINAL_NOTICE_STATUSES = {
    "Approval required",
    "Permission denied",
    "Waiting for user",
    "Error",
}


@dataclass
class TuiMessage:
    role: str
    content: str


@dataclass
class TuiTranscriptEntry:
    kind: str
    key: str


@dataclass
class TuiTool:
    tool_call_id: str
    name: str
    args_preview: str = ""
    status: str = "pending"
    summary: str = ""


@dataclass
class TuiNotice:
    kind: str
    text: str


@dataclass
class TuiState:
    session_id: str = "default"
    thread_id: str = "agent"
    agent_name: str = "XBotv2"
    status: str = "Disconnected"
    messages: list[TuiMessage] = field(default_factory=list)
    tools: dict[str, TuiTool] = field(default_factory=dict)
    notices: list[TuiNotice] = field(default_factory=list)
    transcript: list[TuiTranscriptEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    turn: int = 0
    _tool_transcript_keys: set[str] = field(default_factory=set)

    def apply_frame(self, frame: ProtocolFrame) -> None:
        self.session_id = frame.session_id or self.session_id
        self.thread_id = frame.thread_id or self.thread_id
        self.apply_event({"type": frame.type, "data": frame.payload})

    def apply_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}

        if event_type == "hello_ok":
            self.status = f"Connected to {data.get('server_name') or 'server'}"
        elif event_type == "session_ready":
            self.agent_name = str(data.get("agent_name") or self.agent_name)
            self.status = "Ready"
        elif event_type == "turn_started":
            self.turn = int(data.get("turn") or self.turn or 0)
            self.status = "Running"
        elif event_type == "turn_finished":
            self.turn = int(data.get("turn") or self.turn or 0)
            if self.status not in _TERMINAL_NOTICE_STATUSES:
                self.status = "Ready"
        elif event_type == "assistant_message":
            content = str(data.get("content") or "")
            if content:
                self.append_message("assistant", content)
            self._apply_tool_calls(data.get("tool_calls"))
        elif event_type == "tool_calls_started":
            self._apply_tool_calls(data.get("tool_calls"))
        elif event_type == "tool_result":
            tool = self._tool(
                str(data.get("tool_call_id") or "tool"),
                name=str(data.get("name") or "tool"),
            )
            tool.status = str(data.get("status") or "completed")
            tool.summary = _preview(data.get("content") or data.get("summary") or "")
            self._ensure_tool_transcript(tool.tool_call_id)
        elif event_type == "status":
            self.status = str(data.get("text") or data.get("message") or self.status)
        elif event_type == "client_message":
            self.append_notice("client_message", str(data.get("message") or data))
        elif event_type == "permission_request":
            self.status = "Approval required"
            self.append_notice(
                "permission_request",
                str(data.get("reason") or "Tool approval required."),
            )
        elif event_type == "permission_denied":
            self.status = "Permission denied"
            self.append_notice(
                "permission_denied",
                str(data.get("reason") or "Tool call denied."),
            )
        elif event_type == "user_input_required":
            self.status = "Waiting for user"
            question = str(data.get("question") or "User input required.")
            options = data.get("options")
            if isinstance(options, list) and options:
                question = f"{question} Options: {', '.join(str(item) for item in options)}"
            self.append_notice("user_input_required", question)
        elif event_type == "error":
            self.status = "Error"
            self.errors.append(str(data.get("message") or data))
            self.transcript.append(TuiTranscriptEntry(kind="error", key=str(len(self.errors) - 1)))
        elif event_type == "shutdown_ok":
            self.status = "Shutdown"

    def append_message(self, role: str, content: str) -> None:
        self.messages.append(TuiMessage(role=role, content=content))
        self.transcript.append(TuiTranscriptEntry(kind="message", key=str(len(self.messages) - 1)))

    def append_notice(self, kind: str, text: str) -> None:
        self.notices.append(TuiNotice(kind=kind, text=text))
        self.transcript.append(TuiTranscriptEntry(kind="notice", key=str(len(self.notices) - 1)))

    def lines(self, *, width: int, height: int) -> list[str]:
        width = max(20, width)
        height = max(5, height)
        lines = [
            f"XBotv2  {self.session_id}/{self.thread_id}  {self.status}"[:width],
            f"Agent {self.agent_name}  Turn {self.turn}"[:width],
            "=" * min(width, 200),
        ]

        body_height = max(1, height - 5)
        body = self._transcript_lines(width, body_height) or ["No messages yet."]
        for index in range(body_height):
            lines.append((body[index] if index < len(body) else "")[:width])

        lines.append("-" * min(width, 200))
        lines.append("[Enter] send  /exit quit"[:width])
        return lines[:height]

    def _transcript_lines(self, width: int, height: int) -> list[str]:
        lines: list[str] = []
        for entry in self.transcript:
            if entry.kind == "message":
                try:
                    message = self.messages[int(entry.key)]
                except (ValueError, IndexError):
                    continue
                label = self.agent_name if message.role == "assistant" else "You"
                lines.extend(_wrap(f"{label}> {message.content}", width))
            elif entry.kind == "tool":
                tool = self.tools.get(entry.key)
                if tool is None:
                    continue
                lines.append(shorten(f"Tool {tool.name} [{tool.status}]", width=width, placeholder="..."))
                detail = " | ".join(part for part in (tool.args_preview, tool.summary) if part)
                if detail:
                    lines.extend(_wrap(f"  {detail}", width))
            elif entry.kind == "error":
                try:
                    error = self.errors[int(entry.key)]
                except (ValueError, IndexError):
                    continue
                lines.extend(_wrap(f"Error> {error}", width))
            elif entry.kind == "notice":
                try:
                    notice = self.notices[int(entry.key)]
                except (ValueError, IndexError):
                    continue
                lines.extend(_wrap(f"{_notice_label(notice.kind)}> {notice.text}", width))
        return lines[-height:]

    def _apply_tool_calls(self, tool_calls: Any) -> None:
        if not isinstance(tool_calls, list):
            return
        for index, raw_tool in enumerate(tool_calls):
            if not isinstance(raw_tool, dict):
                continue
            tool_call_id = str(raw_tool.get("id") or raw_tool.get("tool_call_id") or f"tool_{index}")
            tool = self._tool(tool_call_id, name=str(raw_tool.get("name") or "tool"))
            tool.args_preview = _preview(raw_tool.get("args") or raw_tool.get("arguments") or "")
            tool.status = "pending"
            self._ensure_tool_transcript(tool_call_id)

    def _tool(self, tool_call_id: str, *, name: str) -> TuiTool:
        if tool_call_id not in self.tools:
            self.tools[tool_call_id] = TuiTool(tool_call_id=tool_call_id, name=name)
        elif name != "tool":
            self.tools[tool_call_id].name = name
        return self.tools[tool_call_id]

    def _ensure_tool_transcript(self, tool_call_id: str) -> None:
        if tool_call_id in self._tool_transcript_keys:
            return
        self._tool_transcript_keys.add(tool_call_id)
        self.transcript.append(TuiTranscriptEntry(kind="tool", key=tool_call_id))


class CursesTuiClient:
    """Curses UI shell around TerminalSession."""

    def __init__(
        self,
        data_dir: Path | str = "data",
        personality_id: str = "default",
        provider_name: str = "default",
        session_id: str = "default",
        thread_id: str = "agent",
    ) -> None:
        self.session = TerminalSession(
            data_dir=data_dir,
            personality_id=personality_id,
            provider_name=provider_name,
            session_id=session_id,
            thread_id=thread_id,
        )
        self.state = TuiState(session_id=session_id, thread_id=thread_id)
        self._events: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: set[Future] = set()
        self._closed = False

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.session.connect()
        self.state.status = "Ready"
        try:
            await asyncio.to_thread(curses.wrapper, self._run_curses)
        finally:
            self._closed = True
            for future in list(self._pending):
                future.cancel()
            await self.session.disconnect()

    def _run_curses(self, stdscr: Any) -> None:
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        stdscr.timeout(100)
        input_buffer = ""

        while True:
            self._drain_events()
            self._draw(stdscr, input_buffer)
            ch = stdscr.getch()

            if ch == -1:
                continue
            if ch in (10, 13):
                text = input_buffer.strip()
                input_buffer = ""
                if text == "/exit":
                    return
                if text:
                    self._send_text(text)
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                input_buffer = input_buffer[:-1]
            elif 0 <= ch < 256:
                input_buffer += chr(ch)

    def _draw(self, stdscr: Any, input_buffer: str) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        for row, line in enumerate(self.state.lines(width=width, height=max(1, height - 1))):
            try:
                stdscr.addnstr(row, 0, line, max(1, width - 1))
            except curses.error:
                pass
        try:
            stdscr.addnstr(max(0, height - 1), 0, f"> {input_buffer}", max(1, width - 1))
        except curses.error:
            pass
        stdscr.refresh()

    def _send_text(self, text: str) -> None:
        if self._loop is None:
            raise RuntimeError("CursesTuiClient is not running")
        self.state.append_message("user", text)
        future = asyncio.run_coroutine_threadsafe(self._collect_response(text), self._loop)
        self._pending.add(future)
        future.add_done_callback(self._pending.discard)

    async def _collect_response(self, text: str) -> None:
        try:
            async for event in self.session.send_message(text):
                self._events.put(event)
        except BaseException as exc:
            self._events.put(exc)

    def _drain_events(self) -> None:
        while True:
            try:
                item = self._events.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, BaseException):
                self.state.status = "Error"
                self.state.errors.append(str(item))
                self.state.transcript.append(
                    TuiTranscriptEntry(kind="error", key=str(len(self.state.errors) - 1))
                )
                continue
            self.state.apply_event(item)


def _preview(value: Any, *, width: int = 120) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            text = str(value)
    return shorten(text.replace("\n", " "), width=width, placeholder="...")


def _wrap(text: str, width: int) -> list[str]:
    if width <= 0:
        return [""]
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        if len(word) > width:
            if current:
                lines.append(current)
                current = ""
            lines.extend(word[index : index + width] for index in range(0, len(word), width))
            continue
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _notice_label(kind: str) -> str:
    labels = {
        "client_message": "Notice",
        "permission_request": "Approval",
        "permission_denied": "Denied",
        "user_input_required": "Question",
    }
    return labels.get(kind, "Event")
