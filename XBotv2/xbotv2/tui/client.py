"""Protocol-driven state shared by TUI clients."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from textwrap import shorten
from typing import Any


@dataclass
class TuiMessage:
    role: str
    content: str
    ts: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))
    reasoning: str = ""


@dataclass
class TuiTranscriptEntry:
    kind: str
    key: str


@dataclass
class TuiTool:
    tool_call_id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    args_preview: str = ""
    args_streaming: str = ""
    args_finalized: bool = False
    status: str = "pending"
    summary: str = ""
    result: str = ""
    data: Any = None
    error: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    # Wall-clock seconds between ``tool_calls_started`` and
    # ``tool_result``. Set when the result arrives. While pending,
    # the value is the live elapsed (see ``elapsed()``).
    started_at: float = 0.0
    finished_at: float = 0.0
    # Permission state — set when the engine sends a
    # ``permission_request`` for this tool. The TUI renders
    # inline approval choices inside the tool widget instead of
    # creating a separate notice entry.
    permission_pending: bool = False
    permission_request_id: str = ""
    permission_reason: str = ""

    def elapsed(self, now: float | None = None) -> float:
        """Return seconds since the tool started.

        Returns 0.0 if the tool never started (defensive default).
        """

        if self.started_at <= 0:
            return 0.0
        end = self.finished_at if self.finished_at > 0 else (now or self.started_at)
        return max(0.0, end - self.started_at)


@dataclass(slots=True)
class TuiTask:
    task_id: str
    command: str
    cwd: str = ""
    status: str = "pending"
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    output: str = ""
    error: str = ""

    def elapsed(self, now: float | None = None) -> float:
        if self.started_at <= 0:
            return 0.0
        end = self.finished_at or now or time.time()
        return max(0.0, end - self.started_at)


@dataclass
class TuiNotice:
    kind: str
    text: str
    ts: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class TuiState:
    session_id: str = "default"
    thread_id: str = "agent"
    agent_name: str = "XBotv2"
    workspace_root: str = ""
    provider: str = ""
    model: str = ""
    context_window: int = 0
    context_input_tokens: int = 0
    status: str = "Disconnected"
    usage: dict[str, int] = field(default_factory=lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "requests": 0,
    })
    turn_usage: dict[str, int] = field(default_factory=lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "requests": 0,
    })
    messages: list[TuiMessage] = field(default_factory=list)
    tools: dict[str, TuiTool] = field(default_factory=dict)
    tasks: dict[str, TuiTask] = field(default_factory=dict)
    notices: list[TuiNotice] = field(default_factory=list)
    transcript: list[TuiTranscriptEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    turn: int = 0
    turn_active: bool = False
    pending_user_input_payload: dict[str, Any] | None = None
    pending_permission_payload: dict[str, Any] | None = None
    _tool_transcript_keys: set[str] = field(default_factory=set)
    _streaming_assistant_index: int | None = None
    _streaming_tool_ids: dict[int, str] = field(default_factory=dict)
    _changed_tool_ids: set[str] = field(default_factory=set)
    _tool_id_renames: dict[str, str] = field(default_factory=dict)

    def apply_event(self, event: dict[str, Any]) -> None:
        self._changed_tool_ids.clear()
        self._tool_id_renames.clear()
        event_type = str(event.get("type") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}

        if event_type == "hello_ok":
            self.status = f"Connected to {data.get('server_name') or 'server'}"
        elif event_type == "session_ready":
            self.agent_name = str(data.get("agent_name") or self.agent_name)
            self.status = "Ready"
        elif event_type == "turn_started":
            self.turn = int(data.get("turn") or self.turn or 0)
            self.turn_active = True
            self._clear_pending_interactions(tool_status="cancelled")
            self.turn_usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "requests": 0,
            }
            self._streaming_assistant_index = None
            self._streaming_tool_ids.clear()
            self._refresh_status(reset_terminal=True)
        elif event_type == "turn_finished":
            self.turn = int(data.get("turn") or self.turn or 0)
            self.turn_active = False
            self._clear_pending_interactions(tool_status="cancelled")
            self._refresh_status()
        elif event_type == "turn_cancelled":
            self.turn = int(data.get("turn") or self.turn or 0)
            self.turn_active = False
            self._clear_pending_interactions(tool_status="cancelled")
            self.status = "Interrupted"
            self._refresh_status()
        elif event_type == "assistant_message":
            content = str(data.get("content") or "")
            tool_calls = data.get("tool_calls")
            if content.strip():
                if self._streaming_assistant_index is not None:
                    self._streaming_assistant_index = None
                else:
                    self.append_message("assistant", content)
            elif tool_calls:
                # Reset the streaming index so the next LLM call
                # creates a fresh message entry. Reasoning (if any)
                # was already streamed; the tool widget itself tells
                # the user the model is acting.
                self._streaming_assistant_index = None
            self._apply_tool_calls(tool_calls)
            self._streaming_tool_ids.clear()
        elif event_type == "assistant_message_delta":
            content = str(data.get("content") or "")
            reasoning = str(data.get("reasoning") or "")
            if self.turn_active:
                self.status = "Thinking" if reasoning and not content else "Running"
            self.append_assistant_delta(content, reasoning)
        elif event_type == "tool_call_delta":
            if self.turn_active:
                self.status = "Running"
            self._apply_tool_call_delta(data.get("tool_calls"))
        elif event_type == "tool_calls_started":
            if self.turn_active:
                self.status = "Running"
            self._apply_tool_calls(data.get("tool_calls"))
            self._streaming_tool_ids.clear()
        elif event_type == "tool_result":
            tool = self._tool(
                str(data.get("tool_call_id") or "tool"),
                name=str(data.get("name") or "tool"),
            )
            tool.status = str(data.get("status") or "completed")
            content = data.get("content") or data.get("summary") or ""
            tool.result = format_value(content)
            tool.summary = _preview(content)
            tool.data = data.get("data")
            tool.error = data.get("error") if isinstance(data.get("error"), dict) else None
            artifacts = data.get("artifacts")
            tool.artifacts = [
                dict(artifact) for artifact in artifacts or []
                if isinstance(artifact, dict)
            ]
            # Mark the wall-clock end of this tool call so the
            # transcript can show "shell success  0.4s" (per user
            # request: per-tool latency in the entry title).
            tool.finished_at = time.monotonic()
            self._ensure_tool_transcript(tool.tool_call_id)
            self._changed_tool_ids.add(tool.tool_call_id)
        elif event_type == "task_updated":
            task_id = str(data.get("task_id") or "")
            if task_id:
                self.tasks[task_id] = TuiTask(
                    task_id=task_id,
                    command=str(data.get("command") or ""),
                    cwd=str(data.get("cwd") or ""),
                    status=str(data.get("status") or "pending"),
                    created_at=float(data.get("created_at") or 0),
                    started_at=float(data.get("started_at") or 0),
                    finished_at=float(data.get("finished_at") or 0),
                    output=str(data.get("output") or ""),
                    error=str(data.get("error") or ""),
                )
        elif event_type == "usage":
            self._apply_usage(data)
        elif event_type == "status":
            self.status = str(data.get("text") or data.get("message") or self.status)
        elif event_type == "client_message":
            self.append_notice("client_message", str(data.get("message") or data))
        elif event_type == "permission_request":
            self.pending_permission_payload = data
            self._refresh_status()
            tool_call = data.get("tool_call") if isinstance(data.get("tool_call"), dict) else {}
            tool_id = str(tool_call.get("id") or "")
            if tool_id:
                tool = self._tool(
                    tool_id,
                    name=str(tool_call.get("name") or "tool"),
                )
                args = tool_call.get("args")
                if isinstance(args, dict):
                    tool.args = dict(args)
                    tool.args_preview = _preview(args)
                    tool.args_finalized = True
                tool.permission_pending = True
                tool.permission_request_id = str(data.get("request_id") or "")
                tool.permission_reason = str(data.get("reason") or "")
                tool.status = "pending approval"
                self._ensure_tool_transcript(tool_id)
                self._changed_tool_ids.add(tool_id)
        elif event_type == "permission_denied":
            self.status = "Permission denied"
            self.pending_permission_payload = None
            request_id = str(data.get("request_id") or "")
            tool = self._tool_for_permission_request(request_id)
            if tool is not None:
                tool.permission_pending = False
                tool.status = "denied"
                self._changed_tool_ids.add(tool.tool_call_id)
        elif event_type == "user_input_required":
            self.pending_user_input_payload = data
            self._refresh_status()
            question = str(data.get("question") or "User input required.")
            self.append_notice("user_input_required", question, payload=data)
        elif event_type == "user_input_recorded":
            self.pending_user_input_payload = None
            self._refresh_status()
            self.append_notice(
                "user_input_recorded",
                str(data.get("status") or data.get("request_id") or "User input recorded."),
                payload=data,
            )
        elif event_type == "permission_response_recorded":
            self.pending_permission_payload = None
            self._refresh_status()
            request_id = str(data.get("request_id") or "")
            decision = str(data.get("decision") or str(data.get("status") or "approved"))
            tool = self._tool_for_permission_request(request_id)
            if tool is not None:
                tool.permission_pending = False
                tool.status = decision if decision else "approved"
                self._changed_tool_ids.add(tool.tool_call_id)
        elif event_type == "error":
            self._clear_pending_interactions(tool_status="failed")
            self.status = "Error"
            self.errors.append(str(data.get("message") or data))
            self.transcript.append(TuiTranscriptEntry(kind="error", key=str(len(self.errors) - 1)))
        elif event_type == "shutdown_ok":
            self.status = "Shutdown"

    def append_message(self, role: str, content: str) -> None:
        content = _repair_mojibake(content)
        self.messages.append(TuiMessage(role=role, content=content))
        self.transcript.append(TuiTranscriptEntry(kind="message", key=str(len(self.messages) - 1)))

    def restore_history(self, history: list[dict[str, Any]]) -> None:
        """Rebuild the visible transcript from a resumed session."""
        for item in history:
            role = str(item.get("role") or "")
            if role == "user":
                self.append_message("user", str(item.get("content") or ""))
                self.turn += 1
            elif role == "assistant":
                self.apply_event({
                    "type": "assistant_message",
                    "data": {
                        "content": str(item.get("content") or ""),
                        "tool_calls": item.get("tool_calls") or [],
                    },
                })
            elif role == "tool":
                self.apply_event({
                    "type": "tool_result",
                    "data": {
                        "tool_call_id": str(item.get("tool_call_id") or "tool"),
                        "content": str(item.get("content") or ""),
                        "status": str(item.get("status") or "completed"),
                        "data": item.get("data"),
                        "error": item.get("error"),
                        "artifacts": item.get("artifacts") or [],
                    },
                })

    def append_assistant_delta(self, content: str, reasoning: str = "") -> None:
        if not content and not reasoning:
            return
        if self._streaming_assistant_index is None:
            self.append_message("assistant", "")
            self._streaming_assistant_index = len(self.messages) - 1
        try:
            msg = self.messages[self._streaming_assistant_index]
            if content:
                msg.content += content
            if reasoning:
                msg.reasoning += reasoning
        except IndexError:
            self.append_message("assistant", content or "")
            self._streaming_assistant_index = len(self.messages) - 1

    def append_notice(
        self,
        kind: str,
        text: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.notices.append(TuiNotice(kind=kind, text=text, payload=payload or {}))
        self.transcript.append(TuiTranscriptEntry(kind="notice", key=str(len(self.notices) - 1)))

    def _clear_pending_interactions(self, *, tool_status: str) -> None:
        self.pending_user_input_payload = None
        self.pending_permission_payload = None
        for tool in self.tools.values():
            if tool.permission_pending:
                tool.permission_pending = False
                tool.status = tool_status
                self._changed_tool_ids.add(tool.tool_call_id)

    def _refresh_status(self, *, reset_terminal: bool = False) -> None:
        if self.status == "Shutdown":
            return
        if (
            self.status in {"Error", "Interrupted", "Permission denied"}
            and not reset_terminal
        ):
            return
        if self.pending_permission_payload is not None:
            self.status = "Approval required"
        elif self.pending_user_input_payload is not None:
            self.status = "Waiting for user"
        elif self.turn_active:
            self.status = "Running"
        else:
            self.status = "Ready"

    def lines(self, *, width: int, height: int) -> list[str]:
        width = max(20, width)
        height = max(5, height)
        lines = [
            f"XBotv2  {self.session_id}/{self.thread_id}  {self.status}"[:width],
            f"Agent {self.agent_name}  Turn {self.turn}  Tokens {self.usage['total_tokens']}"[:width],
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
                if message.reasoning:
                    lines.extend(_wrap(f"{label} (thinking)> {message.reasoning}", width))
                lines.extend(_wrap(f"{label}> {message.content}", width))
            elif entry.kind == "tool":
                tool = self.tools.get(entry.key)
                if tool is None:
                    continue
                lines.append(shorten(f"Tool {tool.name} [{tool.status}]", width=width, placeholder="..."))
                # Show finalized args (clean dict repr) when available;
                # fall back to the raw streaming buffer so the user
                # still sees something mid-stream. Avoids
                # ``{"command": "cu`` flicker in narrow terminals.
                preview = tool.args_preview if tool.args_finalized else tool.args_streaming
                detail = " | ".join(part for part in (preview, tool.summary) if part)
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
            stream_index = int(raw_tool.get("index") if raw_tool.get("index") is not None else index)
            raw_id = raw_tool.get("tool_call_id") or raw_tool.get("id")
            if raw_id:
                tool_call_id = str(raw_id)
                previous_id = self._streaming_tool_ids.get(stream_index)
                if (
                    previous_id
                    and previous_id != tool_call_id
                    and _is_provisional_tool_id(previous_id)
                ):
                    self._rename_tool(previous_id, tool_call_id)
                self._streaming_tool_ids[stream_index] = tool_call_id
            else:
                tool_call_id = self._streaming_tool_ids.get(stream_index, f"tool_{stream_index}")
                self._streaming_tool_ids.setdefault(stream_index, tool_call_id)
            tool = self._tool(tool_call_id, name=str(raw_tool.get("name") or "tool"))
            # tool_calls_started carries the FINAL parsed args (dict).
            # Replace the streaming preview with the clean dict repr
            # and mark finalized so the title/body no longer show the
            # raw partial JSON string.
            final_args = raw_tool.get("args") or raw_tool.get("arguments")
            if final_args:
                if isinstance(final_args, dict):
                    tool.args = dict(final_args)
                tool.args_preview = _preview(final_args)
                tool.args_finalized = True
            tool.status = "pending"
            # Stamp the start of this tool call only on the FIRST
            # tool_calls_started event for this id — re-firing the
            # same call (e.g. on resume) should not reset the clock.
            if tool.started_at <= 0:
                tool.started_at = time.monotonic()
            self._ensure_tool_transcript(tool_call_id)
            self._changed_tool_ids.add(tool_call_id)

    def _apply_tool_call_delta(self, tool_calls: Any) -> None:
        if not isinstance(tool_calls, list):
            return
        for index, raw_tool in enumerate(tool_calls):
            if not isinstance(raw_tool, dict):
                continue
            stream_index = int(raw_tool.get("index") if raw_tool.get("index") is not None else index)
            raw_id = raw_tool.get("tool_call_id") or raw_tool.get("id")
            if raw_id:
                tool_call_id = str(raw_id)
                previous_id = str(
                    raw_tool.get("replaces_tool_call_id")
                    or self._streaming_tool_ids.get(stream_index)
                    or ""
                )
                if (
                    previous_id
                    and previous_id != tool_call_id
                    and _is_provisional_tool_id(previous_id)
                ):
                    self._rename_tool(previous_id, tool_call_id)
                self._streaming_tool_ids[stream_index] = tool_call_id
            else:
                tool_call_id = self._streaming_tool_ids.get(stream_index, f"tool_{stream_index}")
                self._streaming_tool_ids.setdefault(stream_index, tool_call_id)
            tool = self._tool(tool_call_id, name=str(raw_tool.get("name") or "tool"))
            # Accumulate raw JSON in args_streaming only. The
            # title and body keep args_preview empty until the
            # tool_calls_started event delivers the parsed dict —
            # this prevents the user from seeing half-formed
            # ``{"command": "cu`` in the title mid-stream.
            if tool.args_finalized:
                continue
            args = raw_tool.get("args_delta")
            if args is None:
                args = raw_tool.get("args") or raw_tool.get("arguments") or ""
            if isinstance(args, str):
                tool.args_streaming = f"{tool.args_streaming}{args}"
            elif args:
                tool.args_streaming = str(args)
            tool.status = "pending"
            if tool.started_at <= 0:
                tool.started_at = time.monotonic()
            self._ensure_tool_transcript(tool_call_id)
            self._changed_tool_ids.add(tool_call_id)

    def _apply_usage(self, data: dict[str, Any]) -> None:
        usage = data.get("total") if isinstance(data.get("total"), dict) else data
        delta = data.get("delta") if isinstance(data.get("delta"), dict) else None
        if not isinstance(usage, dict):
            return
        current = delta if isinstance(delta, dict) else usage
        if "input_tokens" in current:
            self.context_input_tokens = int(current.get("input_tokens") or 0)
        for key in ("input_tokens", "output_tokens", "total_tokens", "requests"):
            val = int(usage.get(key) or 0)
            if key in usage:
                self.usage[key] = val
            # When no ``delta`` sub-key exists, treat the flat data
            # itself as the delta — the engine sends one ``usage``
            # event per LLM call, and each event carries the current
            # provider-side consumption, which IS the turn-level delta.
            if isinstance(delta, dict):
                if key in delta:
                    self.turn_usage[key] += int(delta.get(key) or 0)
            elif key in usage:
                self.turn_usage[key] += val

    def _tool(self, tool_call_id: str, *, name: str) -> TuiTool:
        if tool_call_id not in self.tools:
            self.tools[tool_call_id] = TuiTool(tool_call_id=tool_call_id, name=name)
        elif name != "tool":
            self.tools[tool_call_id].name = name
        return self.tools[tool_call_id]

    def _tool_for_permission_request(self, request_id: str) -> TuiTool | None:
        return next(
            (
                tool
                for tool in self.tools.values()
                if tool.permission_request_id == request_id
            ),
            None,
        )

    def _ensure_tool_transcript(self, tool_call_id: str) -> None:
        if tool_call_id in self._tool_transcript_keys:
            return
        self._tool_transcript_keys.add(tool_call_id)
        self.transcript.append(TuiTranscriptEntry(kind="tool", key=tool_call_id))

    def _rename_tool(self, old_id: str, new_id: str) -> None:
        if old_id == new_id or old_id not in self.tools:
            return
        old_tool = self.tools.pop(old_id)
        existing = self.tools.get(new_id)
        if existing is None:
            old_tool.tool_call_id = new_id
            self.tools[new_id] = old_tool
        else:
            if not existing.args:
                existing.args = old_tool.args
            if not existing.args_preview:
                existing.args_preview = old_tool.args_preview
            if not existing.args_streaming:
                existing.args_streaming = old_tool.args_streaming
            if existing.started_at <= 0:
                existing.started_at = old_tool.started_at
        for entry in self.transcript:
            if entry.kind == "tool" and entry.key == old_id:
                entry.key = new_id
        if old_id in self._tool_transcript_keys:
            self._tool_transcript_keys.remove(old_id)
            self._tool_transcript_keys.add(new_id)
        self._tool_id_renames[old_id] = new_id
        self._changed_tool_ids.update({old_id, new_id})


def _is_provisional_tool_id(tool_call_id: str) -> bool:
    return tool_call_id.startswith("tool_")


def _preview(value: Any, *, width: int = 120) -> str:
    """Render a short, single-line-friendly preview of ``value``.

    Newlines are preserved and each line is independently shortened. Tool
    details may be collapsed by the frontend, but their content remains
    available without changing the protocol value.
    """

    text = format_value(value)
    return "\n".join(
        shorten(line, width=width, placeholder="...") for line in text.splitlines() or [""]
    )


def format_value(value: Any, *, indent: int | None = None) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
        )
    except TypeError:
        return str(value)


def _repair_mojibake(text: str) -> str:
    """Repair common UTF-8 bytes decoded as Latin-1/CP1252 mojibake."""

    if not text or not any(marker in text for marker in ("Ã", "Â", "å", "æ", "ç", "è", "é")):
        return text
    try:
        repaired = text.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return text
    return repaired if _cjk_score(repaired) > _cjk_score(text) else text


def _cjk_score(text: str) -> int:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


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
        "user_input_recorded": "Answer",
        "permission_response_recorded": "Approval",
    }
    return labels.get(kind, "Event")


def _parse_permission_decision(text: str) -> dict[str, str]:
    normalized = text.strip().lower()
    parts = normalized.split()
    scope = "once"
    if parts and parts[0] in {"session", "once"}:
        scope = parts.pop(0)
    elif parts and parts[-1] in {"session", "once"}:
        scope = parts.pop()
    decision_text = " ".join(parts) if parts else normalized
    decision = "allow" if decision_text in {"allow", "approve", "approved", "yes", "y"} else "deny"
    return {"decision": decision, "scope": scope}
