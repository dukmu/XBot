"""Protocol-driven state shared by TUI clients."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from textwrap import shorten
from typing import Any


_USAGE_COUNTER_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "requests",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "prompt_cache_write_tokens",
)


def _empty_usage_counters() -> dict[str, int]:
    return {key: 0 for key in _USAGE_COUNTER_KEYS}


def _effective_context_tokens(usage: dict[str, Any], previous: int = 0) -> int:
    if "context_tokens" in usage:
        return int(usage.get("context_tokens") or 0)
    input_keys = (
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "prompt_cache_write_tokens",
    )
    if any(key in usage for key in input_keys):
        return sum(int(usage.get(key) or 0) for key in input_keys)
    return previous


@dataclass
class TuiMessage:
    role: str
    content: str
    ts: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))
    reasoning: str = ""
    streaming: bool = False


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
    images: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    permission_pending: bool = False
    permission_request_id: str = ""
    permission_reason: str = ""

    def elapsed(self, now: float | None = None) -> float:
        if self.started_at <= 0:
            return 0.0
        end = self.finished_at if self.finished_at > 0 else (now or self.started_at)
        return max(0.0, end - self.started_at)


@dataclass(slots=True)
class TuiTask:
    task_id: str
    command: str
    kind: str = "shell"
    cwd: str = ""
    status: str = "pending"
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    output: str = ""
    error: str = ""
    agent: str = ""
    thread_id: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    terminal_since: float = 0.0

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
    model_mode: str = ""
    status_slots: dict[str, str] = field(default_factory=dict)
    context_window: int = 0
    context_input_tokens: int = 0
    status: str = "Disconnected"
    usage: dict[str, int] = field(default_factory=_empty_usage_counters)
    turn_usage: dict[str, int] = field(default_factory=_empty_usage_counters)
    messages: list[TuiMessage] = field(default_factory=list)
    tools: dict[str, TuiTool] = field(default_factory=dict)
    tasks: dict[str, TuiTask] = field(default_factory=dict)
    notices: list[TuiNotice] = field(default_factory=list)
    transcript: list[TuiTranscriptEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    turn: int = 0
    turn_active: bool = False
    compaction_active: bool = False
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
        slots = data.get("status_slots")
        if isinstance(slots, dict):
            self.status_slots = {
                str(name): str(value)
                for name, value in slots.items()
                if str(name).strip() and str(value).strip()
            }

        if event_type == "hello_ok":
            self.status = f"Connected to {data.get('server_name') or 'server'}"
        elif event_type == "session_ready":
            self.agent_name = str(data.get("agent_name") or self.agent_name)
            self.status = "Ready"
        elif event_type == "turn_started":
            self.turn = int(data.get("turn") or self.turn or 0)
            self.turn_active = True
            self._clear_pending_interactions(tool_status="cancelled")
            self.turn_usage = _empty_usage_counters()
            self._streaming_assistant_index = None
            self._streaming_tool_ids.clear()
            self._refresh_status(reset_terminal=True)
        elif event_type == "turn_finished":
            self.turn = int(data.get("turn") or self.turn or 0)
            self.turn_active = False
            self.compaction_active = False
            self._clear_pending_interactions(tool_status="cancelled")
            self._refresh_status()
        elif event_type == "turn_cancelled":
            self.turn = int(data.get("turn") or self.turn or 0)
            self.turn_active = False
            self.compaction_active = False
            self._clear_pending_interactions(tool_status="cancelled")
            self.status = "Interrupted"
            self._refresh_status()
        elif event_type == "assistant_message":
            content = str(data.get("content") or "")
            reasoning = str(data.get("reasoning") or "")
            tool_calls = data.get("tool_calls")
            if content.strip() or reasoning:
                if self._streaming_assistant_index is not None:
                    index = self._streaming_assistant_index
                    self._streaming_assistant_index = None
                    try:
                        message = self.messages[index]
                        if content:
                            message.content = content
                        if reasoning:
                            message.reasoning = reasoning
                        message.streaming = False
                    except IndexError:
                        pass
                else:
                    self.append_message("assistant", content)
                    self.messages[-1].reasoning = reasoning
            elif tool_calls:
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
            tool.data = data.get("data")
            tool.summary = _preview(content)
            tool.error = data.get("error") if isinstance(data.get("error"), dict) else None
            artifacts = data.get("artifacts")
            tool.artifacts = [
                dict(artifact) for artifact in artifacts or []
                if isinstance(artifact, dict)
            ]
            tool.images = [
                dict(image) for image in data.get("images") or []
                if isinstance(image, dict)
            ]
            tool.finished_at = time.monotonic()
            self._ensure_tool_transcript(tool.tool_call_id)
            self._changed_tool_ids.add(tool.tool_call_id)
        elif event_type == "task_updated":
            task_id = str(data.get("task_id") or "")
            if task_id:
                previous = self.tasks.get(task_id)
                status = str(data.get("status") or "pending")
                raw_usage = data.get("usage")
                usage = raw_usage if isinstance(raw_usage, dict) else {}
                terminal_since = previous.terminal_since if previous else 0.0
                if status in {"completed", "stopped"} and terminal_since <= 0:
                    terminal_since = time.monotonic()
                self.tasks[task_id] = TuiTask(
                    task_id=task_id,
                    command=str(data.get("command") or ""),
                    kind=str(data.get("kind") or "shell"),
                    cwd=str(data.get("cwd") or ""),
                    status=status,
                    created_at=float(data.get("created_at") or 0),
                    started_at=float(data.get("started_at") or 0),
                    finished_at=float(data.get("finished_at") or 0),
                    output=str(data.get("output") or ""),
                    error=str(data.get("error") or ""),
                    agent=str(data.get("agent") or ""),
                    thread_id=str(data.get("thread_id") or ""),
                    usage={
                        str(key): int(value or 0)
                        for key, value in usage.items()
                        if isinstance(value, (int, float))
                    },
                    terminal_since=terminal_since,
                )
        elif event_type == "usage":
            self._apply_usage(data)
        elif event_type == "compaction_started":
            self.compaction_active = True
            self._refresh_status()
        elif event_type == "compaction_completed":
            self.compaction_active = False
            self._refresh_status(reset_terminal=True)
            if data.get("reason") == "automatic":
                metrics = data.get("metrics") or {}
                self.append_notice(
                    "compact",
                    "Conversation compacted "
                    f"({metrics.get('history_chars_before', 0)} to "
                    f"{metrics.get('history_chars_after', 0)} characters).",
                    payload=data,
                )
        elif event_type == "compaction_failed":
            self.compaction_active = False
            self._refresh_status(reset_terminal=True)
            if data.get("reason") == "automatic":
                self.append_notice(
                    "compact",
                    f"Automatic compaction failed: {data.get('message') or 'unknown error'}",
                    payload=data,
                )
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
            else:
                permission = (
                    data.get("permission")
                    if isinstance(data.get("permission"), dict)
                    else {}
                )
                detail = (
                    f"{permission.get('tool') or 'tool'} "
                    f"{permission.get('params') or {}}"
                )
                reason = str(data.get("reason") or "")
                self.append_notice(
                    "permission_request",
                    f"{detail}\n{reason}" if reason else detail,
                    payload=data,
                )
        elif event_type == "permission_denied":
            self.pending_permission_payload = None
            request_id = str(data.get("request_id") or "")
            tool = self._tool_for_permission_request(request_id)
            if tool is not None:
                tool.permission_pending = False
                tool.status = "denied"
                self._changed_tool_ids.add(tool.tool_call_id)
            self._refresh_status(reset_terminal=True)
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
        elif event_type == "history_updated":
            history = data.get("history")
            if isinstance(history, list):
                self.restore_history(history)
            self._refresh_status()
        elif event_type == "agent_configured":
            if data.get("agent_name"):
                self.agent_name = str(data["agent_name"])
            if data.get("provider"):
                self.provider = str(data["provider"])
            if data.get("model"):
                self.model = str(data["model"])
            if "model_mode" in data:
                self.model_mode = str(data["model_mode"] or "")
            if "context_window" in data:
                self.context_window = int(data["context_window"] or 0)
            self._refresh_status()
        elif event_type == "error":
            self._clear_pending_interactions(tool_status="failed")
            self.turn_active = False
            self.compaction_active = False
            if self._streaming_assistant_index is not None:
                try:
                    self.messages[self._streaming_assistant_index].streaming = False
                except IndexError:
                    pass
            self._streaming_assistant_index = None
            self.status = "Error"
            self.errors.append(str(data.get("message") or data))
            self.transcript.append(TuiTranscriptEntry(kind="error", key=str(len(self.errors) - 1)))
        elif event_type == "shutdown_ok":
            self.status = "Shutdown"

    def append_message(self, role: str, content: str) -> None:
        self.messages.append(TuiMessage(role=role, content=content))
        self.transcript.append(TuiTranscriptEntry(kind="message", key=str(len(self.messages) - 1)))

    def restore_history(self, history: list[dict[str, Any]]) -> None:
        """Rebuild the visible transcript from a resumed session."""
        self.reset_history()
        for item in history:
            role = str(item.get("role") or "")
            if role == "user":
                content = str(item.get("content") or "")
                runtime = item.get("runtime")
                if isinstance(runtime, dict):
                    source = str(runtime.get("source") or "runtime")
                    event = str(runtime.get("event") or "message")
                    self.notices.append(TuiNotice(
                        kind=f"{source}:{event}",
                        text=f"{source} {event}",
                        payload=runtime,
                    ))
                    self.transcript.append(TuiTranscriptEntry(
                        kind="notice",
                        key=str(len(self.notices) - 1),
                    ))
                    continue
                images = item.get("images") or []
                if images:
                    labels = [
                        str(image.get("media_type") or "image")
                        for image in images if isinstance(image, dict)
                    ]
                    content = f"{content}\n\nAttachments: {', '.join(labels)}".strip()
                self.append_message("user", content)
                self.turn += 1
            elif role == "assistant":
                self.apply_event({
                    "type": "assistant_message",
                    "data": {
                        "content": str(item.get("content") or ""),
                        "reasoning": str(item.get("reasoning") or ""),
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
                        "images": item.get("images") or [],
                    },
                })

    def reset_history(self) -> None:
        """Clear conversation-derived state before a new history snapshot."""
        self.messages.clear()
        self.tools.clear()
        self.notices.clear()
        self.errors.clear()
        self.transcript.clear()
        self._tool_transcript_keys.clear()
        self._streaming_assistant_index = None
        self._streaming_tool_ids.clear()
        self._changed_tool_ids.clear()
        self._tool_id_renames.clear()
        self.pending_user_input_payload = None
        self.pending_permission_payload = None
        self.turn = 0
        self.turn_active = False
        self.compaction_active = False
        self.turn_usage = _empty_usage_counters()

    def append_assistant_delta(self, content: str, reasoning: str = "") -> None:
        if not content and not reasoning:
            return
        if self._streaming_assistant_index is None:
            self.append_message("assistant", "")
            self._streaming_assistant_index = len(self.messages) - 1
        try:
            msg = self.messages[self._streaming_assistant_index]
            msg.streaming = True
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
        elif self.compaction_active:
            self.status = "Compacting"
        elif self.turn_active:
            self.status = "Running"
        else:
            self.status = "Ready"

    def prune_finished_tasks(
        self,
        *,
        now: float | None = None,
        retention_seconds: float = 3.0,
    ) -> bool:
        """Remove successful/stopped tasks after a short visible grace period."""
        current = time.monotonic() if now is None else now
        expired = [
            task_id
            for task_id, task in self.tasks.items()
            if task.status in {"completed", "stopped"}
            and task.terminal_since > 0
            and current - task.terminal_since >= retention_seconds
        ]
        for task_id in expired:
            self.tasks.pop(task_id, None)
        return bool(expired)

    def _apply_tool_calls(self, tool_calls: Any) -> None:
        if not isinstance(tool_calls, list):
            return
        for index, raw_tool in enumerate(tool_calls):
            if not isinstance(raw_tool, dict):
                continue
            tool_call_id, tool = self._streaming_tool(raw_tool, index)
            final_args = raw_tool.get("args") or raw_tool.get("arguments")
            if final_args:
                if isinstance(final_args, dict):
                    tool.args = dict(final_args)
                tool.args_preview = _preview(final_args)
                tool.args_finalized = True
            self._mark_tool_pending(tool)

    def _apply_tool_call_delta(self, tool_calls: Any) -> None:
        if not isinstance(tool_calls, list):
            return
        for index, raw_tool in enumerate(tool_calls):
            if not isinstance(raw_tool, dict):
                continue
            _, tool = self._streaming_tool(raw_tool, index)
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
            self._mark_tool_pending(tool)

    def _streaming_tool(
        self,
        raw: dict[str, Any],
        default_index: int,
    ) -> tuple[str, TuiTool]:
        index = int(raw.get("index") if raw.get("index") is not None else default_index)
        raw_id = raw.get("tool_call_id") or raw.get("id")
        tool_call_id = str(raw_id or self._streaming_tool_ids.get(index) or f"tool_{index}")
        previous_id = str(
            raw.get("replaces_tool_call_id")
            or self._streaming_tool_ids.get(index)
            or ""
        )
        if (
            previous_id
            and previous_id != tool_call_id
            and _is_provisional_tool_id(previous_id)
        ):
            self._rename_tool(previous_id, tool_call_id)
        self._streaming_tool_ids[index] = tool_call_id
        return tool_call_id, self._tool(
            tool_call_id, name=str(raw.get("name") or "tool")
        )

    def _mark_tool_pending(self, tool: TuiTool) -> None:
        tool.status = "pending"
        if tool.started_at <= 0:
            tool.started_at = time.monotonic()
        self._ensure_tool_transcript(tool.tool_call_id)
        self._changed_tool_ids.add(tool.tool_call_id)

    def _apply_usage(self, data: dict[str, Any]) -> None:
        self.context_input_tokens = _effective_context_tokens(
            data, self.context_input_tokens
        )
        for key in _USAGE_COUNTER_KEYS:
            if key not in data:
                continue
            value = int(data.get(key) or 0)
            self.usage[key] += value
            self.turn_usage[key] += value

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
