"""Protocol-driven state shared by TUI clients."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from textwrap import shorten
from pydantic import JsonValue

from XBotv2.core.usage import INPUT_USAGE_FIELDS, USAGE_COUNTER_FIELDS

def _empty_usage_counters() -> dict[str, int]:
    return {key: 0 for key in USAGE_COUNTER_FIELDS}


def _effective_context_tokens(usage: dict[str, JsonValue], previous: int = 0) -> int:
    if "context_tokens" in usage:
        return int(usage.get("context_tokens") or 0)
    if any(key in usage for key in INPUT_USAGE_FIELDS):
        return sum(int(usage.get(key) or 0) for key in INPUT_USAGE_FIELDS)
    return previous


@dataclass
class TuiMessage:
    role: str
    content: str
    ts: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))
    reasoning: str = ""
    streaming: bool = False
    message_id: str = ""


@dataclass
class TuiTranscriptEntry:
    kind: str
    key: str


@dataclass
class TuiTool:
    tool_call_id: str
    name: str
    args: dict[str, JsonValue] = field(default_factory=dict)
    args_preview: str = ""
    args_streaming: str = ""
    args_finalized: bool = False
    status: str = "pending"
    summary: str = ""
    result: str = ""
    data: JsonValue = None
    error: dict[str, JsonValue] | None = None
    artifacts: list[dict[str, JsonValue]] = field(default_factory=list)
    images: list[dict[str, JsonValue]] = field(default_factory=list)
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
class TuiJob:
    job_id: str
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
    payload: dict[str, JsonValue] = field(default_factory=dict)


@dataclass
class TuiState:
    session_id: str = "default"
    session_title: str = ""
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
    tasks: dict[str, TuiJob] = field(default_factory=dict)
    notices: list[TuiNotice] = field(default_factory=list)
    transcript: list[TuiTranscriptEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    turn: int = 0
    turn_active: bool = False
    compaction_active: bool = False
    pending_user_input_payload: dict[str, JsonValue] | None = None
    pending_permission_payload: dict[str, JsonValue] | None = None
    # Monotonic counters of what has been evicted.  Consumers (the transcript
    # surface) shift their window and caches by the delta instead of
    # re-deriving positions from list lengths.
    evicted_messages: int = 0
    evicted_notices: int = 0
    evicted_errors: int = 0
    evicted_transcript: int = 0
    # Entries removed from the newest end (the reader paged into history).
    evicted_transcript_tail: int = 0
    inserted_messages: int = 0
    inserted_transcript: int = 0
    # Direction of the window.  While following the tail, live output is
    # appended and the oldest entries are evicted; once the reader pages into
    # history the window keeps their position, live output is counted in
    # ``pending_newer``, and eviction comes from the newest end instead.
    at_tail: bool = True
    pending_newer: int = 0
    # An interrupt has been accepted and the turn has not ended yet.  While it
    # holds, live progress frames must not report the turn as merely running:
    # that is how a finished turn looked like it had resumed.
    interrupt_requested: bool = False
    # Bumped whenever the window is replaced (a resnapshot or a session
    # switch).  A page fetched before the reset must not be spliced into the
    # new window, which is how fast scrolling used to duplicate and lose rows.
    revision: int = 0
    # Cursor for the page older than the retained window.  ``None`` while
    # unset means "ask the server for the newest page first": the page is
    # de-duplicated by message id, so an anchor that is newer than the retained
    # front still reaches the records the client evicted.
    older_cursor: str | None = None
    _tool_transcript_keys: set[str] = field(default_factory=set)
    _streaming_assistant_index: int | None = None
    _streaming_tool_ids: dict[int, str] = field(default_factory=dict)
    _changed_tool_ids: set[str] = field(default_factory=set)
    _tool_id_renames: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.session_title:
            self.session_title = self.session_id

    def apply_event(self, event: dict[str, JsonValue]) -> None:
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

        # Handshake/ready state is driven by the connection path
        # (``_connect``/session snapshot); the protocol emits no
        # ``hello_ok``/``session_ready``/``status``/``shutdown_ok`` frames.
        if event_type == "turn_started":
            self.turn = int(data.get("turn") or self.turn or 0)
            self.turn_active = True
            self.interrupt_requested = False
            self._clear_pending_interactions(tool_status="cancelled")
            self.turn_usage = _empty_usage_counters()
            self._streaming_assistant_index = None
            self._streaming_tool_ids.clear()
            self._refresh_status(reset_terminal=True)
        elif event_type == "turn_finished":
            self.turn = int(data.get("turn") or self.turn or 0)
            self.turn_active = False
            self.interrupt_requested = False
            self.compaction_active = False
            self._clear_pending_interactions(tool_status="error")
            self._finish_pending_tools("error")
            self._refresh_status()
        elif event_type == "turn_cancelled":
            self.turn = int(data.get("turn") or self.turn or 0)
            self.turn_active = False
            self.interrupt_requested = False
            self.compaction_active = False
            self._clear_pending_interactions(tool_status="cancelled")
            self._finish_pending_tools("cancelled")
            self.status = "Interrupted"
            self._refresh_status()
        elif event_type == "assistant_message":
            content = str(data.get("content") or "")
            reasoning = str(data.get("reasoning") or "")
            message_id = str(data.get("id") or "")
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
                        if message_id:
                            message.message_id = message_id
                    except IndexError:
                        pass
                elif message_id and (
                    existing_index := self._message_index(message_id)
                ) is not None:
                    # A durable page or a replayed thread stream already has
                    # this assistant message. Update in place; never append a
                    # second copy for the same server message id.
                    message = self.messages[existing_index]
                    if content:
                        message.content = content
                    if reasoning:
                        message.reasoning = reasoning
                    message.streaming = False
                else:
                    self.append_message(
                        "assistant",
                        content,
                        message_id=message_id,
                    )
                    self.messages[-1].reasoning = reasoning
            elif tool_calls:
                self._streaming_assistant_index = None
            self._apply_tool_calls(tool_calls)
            self._streaming_tool_ids.clear()
        elif event_type == "assistant_message_delta":
            content = str(data.get("content") or "")
            reasoning = str(data.get("reasoning") or "")
            if self.turn_active:
                self._set_live_status(
                    "Thinking" if reasoning and not content else "Running"
                )
            self.append_assistant_delta(content, reasoning)
        elif event_type == "tool_call_delta":
            if self.turn_active:
                self._set_live_status("Running")
            self._apply_tool_call_delta(data.get("tool_calls"))
        elif event_type == "tool_calls_started":
            if self.turn_active:
                self._set_live_status("Running")
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
        elif event_type == "job_updated":
            job_id = str(data.get("job_id") or "")
            if job_id:
                previous = self.tasks.get(job_id)
                status = str(data.get("status") or "pending")
                raw_usage = data.get("usage")
                usage = raw_usage if isinstance(raw_usage, dict) else {}
                terminal_since = previous.terminal_since if previous else 0.0
                if status in {"completed", "stopped"} and terminal_since <= 0:
                    terminal_since = time.monotonic()
                self.tasks[job_id] = TuiJob(
                    job_id=job_id,
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
            metrics = data.get("metrics") or {}
            self.append_notice(
                "compact",
                "Conversation compacted "
                f"({metrics.get('history_chars_before', 0)} to "
                f"{metrics.get('history_chars_after', 0)} characters, "
                f"{metrics.get('messages_before', 0)} to "
                f"{metrics.get('messages_after', 0)} messages)",
                payload=data,
            )
        elif event_type == "compaction_failed":
            self.compaction_active = False
            self._refresh_status(reset_terminal=True)
            if data.get("automatic") is True:
                self.append_notice(
                    "compact",
                    f"Automatic compaction failed: {data.get('message') or 'unknown error'}",
                    payload=data,
                )
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
            self.interrupt_requested = False
            if self._streaming_assistant_index is not None:
                try:
                    self.messages[self._streaming_assistant_index].streaming = False
                except IndexError:
                    pass
            self._streaming_assistant_index = None
            self.status = "Error"
            self.record_error(str(data.get("message") or data))

    def _set_live_status(self, status: str) -> None:
        """Show live progress, unless an interrupt is waiting to land."""
        if self.interrupt_requested:
            return
        self.status = status

    def _trim_state(self) -> None:
        """Bound the retained conversation after a mutation.

        While the reader follows the tail the oldest payloads are evicted; while
        they are inside history the newest are, so paging back keeps what they
        are reading and never grows the window.

        Each index-keyed payload list is evicted from the front (oldest first),
        and the transcript keys of the same kind are renumbered so a retained
        key still resolves to the payload it was created for.  Tools are keyed
        by id, so their cache is dropped from the front of the terminal ones and
        the surviving transcript entry renders nothing once its payload is gone.

        ``evicted_transcript`` is a monotonic count of transcript entries that
        were removed.  A renderer holding positions in the transcript cannot
        derive that from lengths, so it re-anchors when the count moves.
        """
        if not self.at_tail:
            self._trim_tail()
            return
        for container, kind, cap, counter in (
            (self.messages, "message", _MAX_STATE_MESSAGES, "evicted_messages"),
            (self.notices, "notice", _MAX_STATE_NOTICES, "evicted_notices"),
            (self.errors, "error", _MAX_STATE_ERRORS, "evicted_errors"),
        ):
            dropped = self._trim_payloads(container, kind, cap)
            if dropped:
                setattr(self, counter, getattr(self, counter) + dropped)
        excess = len(self.tools) - _MAX_STATE_TOOLS
        if excess > _TRIM_SLACK:
            for tool_id in [
                tool_id
                for tool_id, tool in self.tools.items()
                if tool.status not in ("pending", "running") and not tool.permission_pending
            ][:excess]:
                self.tools.pop(tool_id, None)
                self._tool_transcript_keys.discard(tool_id)
        excess = len(self.transcript) - _MAX_STATE_TRANSCRIPT
        if excess > _TRIM_SLACK:
            del self.transcript[:excess]
            self.evicted_transcript += excess

    def _trim_payloads(self, container: list[object], kind: str, cap: int) -> int:
        excess = len(container) - cap
        if excess <= _TRIM_SLACK:
            return 0
        del container[:excess]
        kept: list[TuiTranscriptEntry] = []
        for entry in self.transcript:
            if entry.kind != kind:
                kept.append(entry)
                continue
            try:
                index = int(entry.key) - excess
            except ValueError:  # pragma: no cover - keys of this kind are indices
                kept.append(entry)
                continue
            if index < 0:
                continue
            entry.key = str(index)
            kept.append(entry)
        self.evicted_transcript += len(self.transcript) - len(kept)
        self.transcript[:] = kept
        if kind == "message" and self._streaming_assistant_index is not None:
            shifted = self._streaming_assistant_index - excess
            self._streaming_assistant_index = shifted if shifted >= 0 else None
        return excess

    def _trim_tail(self) -> None:
        """Drop the newest retained entries while the reader is in history.

        Only the newest record of a kind is removed, so the surviving index
        keys stay valid without renumbering.  A payload is dropped with its
        entry; if it is not the last of its container it is left for its own
        cap to reclaim.
        """
        while len(self.transcript) > _MAX_STATE_TRANSCRIPT:
            entry = self.transcript.pop()
            self.evicted_transcript_tail += 1
            if entry.kind == "message":
                index = _index_key(entry.key)
                if (
                    index is not None
                    and index == len(self.messages) - 1
                    and index != self._streaming_assistant_index
                ):
                    self.messages.pop()
            elif entry.kind == "notice":
                index = _index_key(entry.key)
                if index is not None and index == len(self.notices) - 1:
                    self.notices.pop()
            elif entry.kind == "error":
                index = _index_key(entry.key)
                if index is not None and index == len(self.errors) - 1:
                    self.errors.pop()
            elif entry.kind == "tool":
                self.tools.pop(entry.key, None)
                self._tool_transcript_keys.discard(entry.key)
        excess = len(self.tools) - _MAX_STATE_TOOLS
        if excess > 0:
            for tool_id in list(self.tools)[-excess:]:
                tool = self.tools.get(tool_id)
                if tool is not None and tool.status in ("pending", "running"):
                    continue
                self.tools.pop(tool_id, None)
                self._tool_transcript_keys.discard(tool_id)

    def prepend_history(self, messages: list[dict[str, JsonValue]]) -> int:
        """Insert an older page in front of the retained window.

        Records already retained are skipped: the cursor can be newer than the
        window front, so a page may overlap the window, and re-adding those
        messages would duplicate them.  The unseen ids are exactly the ones
        older than the front, so prepending them in page order stays
        chronological.  Returns how many were inserted.
        """
        known = {message.message_id for message in self.messages if message.message_id}
        fresh = [
            TuiMessage(
                role=role,
                content=str(item.get("content") or ""),
                reasoning=str(item.get("reasoning") or ""),
                message_id=str(item.get("message_id") or ""),
            )
            for item in messages
            for role in [str(item.get("role") or "")]
            if role in ("user", "assistant")
            and not (str(item.get("message_id") or "") in known)
        ]
        if not fresh:
            return 0
        self.messages[0:0] = fresh
        for entry in self.transcript:
            if entry.kind == "message":
                index = _index_key(entry.key)
                if index is not None:
                    entry.key = str(index + len(fresh))
        self.transcript[0:0] = [
            TuiTranscriptEntry(kind="message", key=str(index))
            for index in range(len(fresh))
        ]
        if self._streaming_assistant_index is not None:
            self._streaming_assistant_index += len(fresh)
        self.inserted_messages += len(fresh)
        self.inserted_transcript += len(fresh)
        self.at_tail = False
        self._trim_state()
        return len(fresh)

    def record_error(self, message: str) -> str:
        """Append an error entry and return its transcript key."""
        if not self.at_tail:
            self.pending_newer += 1
            return ""
        self.errors.append(message)
        key = str(len(self.errors) - 1)
        self.transcript.append(TuiTranscriptEntry(kind="error", key=key))
        self._trim_state()
        return key

    def record_notice(self, notice: TuiNotice) -> str:
        """Append a notice and return its transcript key."""
        if not self.at_tail:
            self.pending_newer += 1
            return ""
        self.notices.append(notice)
        key = str(len(self.notices) - 1)
        self.transcript.append(TuiTranscriptEntry(kind="notice", key=key))
        self._trim_state()
        return key

    def _message_index(self, message_id: str) -> int | None:
        if not message_id:
            return None
        for index, message in enumerate(self.messages):
            if message.message_id == message_id:
                return index
        return None

    def append_message(
        self,
        role: str,
        content: str,
        *,
        message_id: str = "",
    ) -> None:
        if not self.at_tail:
            self.pending_newer += 1
            return
        existing = self._message_index(message_id) if message_id else None
        if existing is not None:
            self.messages[existing].content = content
            self.messages[existing].streaming = False
            return
        self.messages.append(
            TuiMessage(role=role, content=content, message_id=message_id)
        )
        self.transcript.append(TuiTranscriptEntry(kind="message", key=str(len(self.messages) - 1)))
        self._trim_state()

    def append_runtime_message(self, data: dict[str, JsonValue]) -> bool:
        """Record one injected harness turn as a notice, not human input.

        Injected reminders and goal rounds are user-role messages carrying
        ``runtime`` provenance; rendering them as typed input would invent a
        human message. Returns False when the event has no provenance so
        callers keep the ordinary human-message path.
        """
        runtime = data.get("runtime")
        if not isinstance(runtime, dict):
            return False
        source = str(runtime.get("source") or "runtime")
        event = str(runtime.get("event") or "message")
        self.record_notice(TuiNotice(
            kind=f"{source}:{event}",
            text=f"{source} {event}",
            payload=runtime,
        ))
        return True

    def restore_history(
        self,
        history: list[dict[str, JsonValue]],
        *,
        older_cursor: str | None = None,
    ) -> None:
        """Rebuild the visible transcript from a resumed session.

        This is also the re-anchor path: the window is replaced by the newest
        page and the direction returns to the live tail.
        """
        self.reset_history()
        self.revision += 1
        self.older_cursor = older_cursor
        for item in history:
            self.apply_history_item(item)

    def apply_history_item(self, item: dict[str, JsonValue]) -> None:
        """Append one persisted history record.

        Restoring a page and appending the newest records of a read-only view
        must agree, so both go through this one place.
        """
        role = str(item.get("role") or "")
        if role == "user":
            content = str(item.get("content") or "")
            if self.append_runtime_message(item):
                return
            images = item.get("images") or []
            if images:
                labels = [
                    str(image.get("media_type") or "image")
                    for image in images if isinstance(image, dict)
                ]
                content = f"{content}\n\nAttachments: {', '.join(labels)}".strip()
            self.append_message(
                "user",
                content,
                message_id=str(
                    item.get("message_id")
                    or item.get("id")
                    or item.get("input_id")
                    or ""
                ),
            )
            self.turn += 1
        elif role == "assistant":
            self.apply_event({
                "type": "assistant_message",
                "data": {
                    "id": str(
                        item.get("message_id") or item.get("id") or ""
                    ),
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
        self.evicted_messages = 0
        self.evicted_notices = 0
        self.evicted_errors = 0
        self.evicted_transcript = 0
        self.evicted_transcript_tail = 0
        self.inserted_messages = 0
        self.inserted_transcript = 0
        self.at_tail = True
        self.pending_newer = 0
        self.interrupt_requested = False
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
        payload: dict[str, JsonValue] | None = None,
    ) -> None:
        self.record_notice(TuiNotice(kind=kind, text=text, payload=payload or {}))

    def _clear_pending_interactions(self, *, tool_status: str) -> None:
        self.pending_user_input_payload = None
        self.pending_permission_payload = None
        for tool in self.tools.values():
            if tool.permission_pending:
                tool.permission_pending = False
                if tool.status in {"pending", "running", "pending approval"}:
                    tool.status = tool_status
                    tool.finished_at = time.monotonic()
                    self._changed_tool_ids.add(tool.tool_call_id)

    def _finish_pending_tools(self, status: str) -> None:
        for tool in self.tools.values():
            if tool.status in {"pending", "running", "pending approval"}:
                tool.permission_pending = False
                tool.status = status
                tool.finished_at = time.monotonic()
                self._changed_tool_ids.add(tool.tool_call_id)

    def _refresh_status(self, *, reset_terminal: bool = False) -> None:
        if (
            self.status in {"Error", "Interrupted", "Permission denied"}
            and not reset_terminal
        ):
            return
        if self.interrupt_requested and not reset_terminal:
            # The request is accepted but the turn has not ended; whatever
            # arrives meanwhile must not claim it is running again.
            self.status = "Interrupting..."
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
            job_id
            for job_id, task in self.tasks.items()
            if task.status in {"completed", "stopped"}
            and task.terminal_since > 0
            and current - task.terminal_since >= retention_seconds
        ]
        for job_id in expired:
            self.tasks.pop(job_id, None)
        return bool(expired)

    def _apply_tool_calls(self, tool_calls: JsonValue) -> None:
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

    def _apply_tool_call_delta(self, tool_calls: JsonValue) -> None:
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
        raw: dict[str, JsonValue],
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

    def _apply_usage(self, data: dict[str, JsonValue]) -> None:
        self.context_input_tokens = _effective_context_tokens(
            data, self.context_input_tokens
        )
        for key in USAGE_COUNTER_FIELDS:
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
        if not self.at_tail:
            self.pending_newer += 1
            return
        self._tool_transcript_keys.add(tool_call_id)
        self.transcript.append(TuiTranscriptEntry(kind="tool", key=tool_call_id))
        self._trim_state()

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
        if existing is not None and old_id in self._tool_transcript_keys:
            # Replay/stream reconciliation: the final tool call already has
            # a durable transcript entry, so the provisional entry must be
            # folded away instead of renamed into a second identical key.
            self.transcript[:] = [
                entry
                for entry in self.transcript
                if not (entry.kind == "tool" and entry.key == old_id)
            ]
            self._tool_transcript_keys.discard(old_id)
        else:
            for entry in self.transcript:
                if entry.kind == "tool" and entry.key == old_id:
                    entry.key = new_id
            if old_id in self._tool_transcript_keys:
                self._tool_transcript_keys.remove(old_id)
                self._tool_transcript_keys.add(new_id)
        self._tool_id_renames[old_id] = new_id
        self._changed_tool_ids.update({old_id, new_id})


# State-side ceilings.  The client keeps a bounded window of the conversation
# rather than a copy of it: the oldest payloads are evicted from the front and
# the transcript entries that referenced them go with them.  The rendered
# window (``TranscriptSurface``) is smaller still, so scrolling back stays
# inside retained state and only reaching further needs a server page.
_MAX_STATE_MESSAGES = 400
_MAX_STATE_NOTICES = 200
_MAX_STATE_ERRORS = 100
_MAX_STATE_TOOLS = 300
_MAX_STATE_TRANSCRIPT = 600
# Evict in batches: one renumber pass per batch keeps the amortized cost of a
# mutation independent of both history length and window size.
_TRIM_SLACK = 200


def history_items_from_trajectory(
    records: list[dict[str, JsonValue]],
) -> list[dict[str, JsonValue]]:
    """Flatten trajectory records into the shape :meth:`TuiState.restore_history` reads.

    The transcript is seeded from a message page, but paging back reads the
    durable trajectory so the anchor survives a client restart.  Only message
    records carry conversation content; events and compactions are already
    represented by the notices this client holds.
    """
    items: list[dict[str, JsonValue]] = []
    for record in records:
        if str(record.get("kind") or "") != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        item = dict(message)
        item["message_id"] = str(record.get("message_id") or message.get("message_id") or "")
        items.append(item)
    return items


def _index_key(key: object) -> int | None:
    """The payload index a transcript key names, or None if it is not one."""
    try:
        return int(str(key))
    except (TypeError, ValueError):
        return None


def _is_provisional_tool_id(tool_call_id: str) -> bool:
    return tool_call_id.startswith("tool_")


def _preview(value: JsonValue, *, width: int = 120) -> str:
    """Render a short, single-line-friendly preview of ``value``.

    Newlines are preserved and each line is independently shortened. Tool
    details may be collapsed by the frontend, but their content remains
    available without changing the protocol value.
    """

    text = format_value(value)
    return "\n".join(
        shorten(line, width=width, placeholder="...") for line in text.splitlines() or [""]
    )


def format_value(value: JsonValue, *, indent: int | None = None) -> str:
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
