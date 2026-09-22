"""The session reducer: the only writer of conversation and status state.

``reduce(state, event)`` is the single entry point. It performs no IO, imports no
Textual, and is the only place any status fact is assigned -- the old client wrote
its status string from nineteen call sites, which is how a running turn ended up
displayed as Ready.

Events carry their protocol payloads, so this module never restates a wire shape:
it reads ``event.payload`` and applies it.

Invariants this module is responsible for:

* a streamed assistant entry is always the *suffix* of the timeline, and a user
  interjection commits that suffix before landing, so streamed text can never be
  appended into an entry that sits above the user's message;
* entries are only ever addressed by their stable id, so nothing is rewritten
  because a list position moved;
* nothing the server sends is dropped without a visible trace: unknown events
  raise, and a rejected frame is a rejection rather than a skip.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Mapping

from XBotv2.compact.protocol import (
    CompactionCompletedData,
    CompactionFailedData,
    CompactionStartedData,
)
from XBotv2.core.usage import INPUT_USAGE_FIELDS, USAGE_COUNTER_FIELDS, UsageData
from XBotv2.interactions.protocol import UserInputRequiredData
from XBotv2.jobs.contracts import JobSnapshot
from XBotv2.permissions.protocol import PermissionRequestData
from XBotv2.session.contracts import PendingInputData, SessionHistoryItem
from XBotv2.session.protocol import OpenSessionResponse
from XBotv2.tui.events import (
    AssistantCompleted,
    AssistantDelta,
    ClientNotice,
    CompactionChanged,
    ConnectionChanged,
    ErrorFrame,
    HistoryReplaced,
    InteractionOpened,
    InteractionRequest,
    InteractionResolved,
    InterruptAsked,
    InterruptSettled,
    JobCompletionNotice,
    JobUpdated,
    LocalNotice,
    TranscriptCleared,
    QueueReplaced,
    SessionConfigured,
    SnapshotAdopted,
    StatusSlotsUpdated,
    StreamGapDetected,
    ToolCallsStarted,
    ToolResult,
    TurnCancelled,
    TurnFinished,
    TurnStarted,
    UiEvent,
    UsageUpdated,
    UserInputFailed,
    UserInputSubmitted,
    UserMessagePublished,
    ThreadRead,
)
from XBotv2.tui.status import Interaction, Interrupt, ServerTurn, StatusFacts
from XBotv2.tui.timeline import (
    AssistantEntry,
    Delivery,
    Entry,
    ErrorEntry,
    NoticeEntry,
    Timeline,
    ToolEntry,
    ToolStatus,
    UserEntry,
)

# A tool is unfinished until a result arrives; these are the two client-side
# states, the other four are the server's own result statuses.
_UNFINISHED_TOOL_STATUSES: frozenset[str] = frozenset({"pending", "running"})
_FINAL_TOOL_STATUS_BY_EVENT: Mapping[str, ToolStatus] = {
    "turn_finished": "cancelled",
    "turn_cancelled": "cancelled",
    "error": "error",
}

_RUNNING_JOB_STATUSES: frozenset[str] = frozenset({"pending", "running"})


# ``ThreadSummary.kind`` is the authority for these two words
# (``tests/tui/test_reuse.py`` pins them to the wire's Literal).
MAIN_THREAD_KIND = "main"
SUBAGENT_THREAD_KIND = "subagent"


@dataclass
class SessionState:
    """Everything the UI needs to render one attached thread.

    Mutable on purpose: the timeline is a long-lived ordered map, and copying it
    per event would reintroduce the per-update cost this rewrite removes.
    ``reduce`` still returns the same object so callers cannot forget to rebind.
    """

    facts: StatusFacts = field(default_factory=StatusFacts)
    timeline: Timeline = field(default_factory=Timeline)
    turn: int = 0
    # When the open turn started, so a renderer can show its duration without
    # keeping a clock of its own.
    turn_started_at: float = 0.0
    stream_entry_id: str | None = None
    queue: tuple[PendingInputData, ...] = ()
    jobs: dict[str, JobSnapshot] = field(default_factory=dict)
    # Unanswered prompts by request id. ``facts.interaction`` is derived from
    # this, so several concurrent prompts cannot lose one another.
    pending_interactions: dict[str, InteractionRequest] = field(default_factory=dict)
    status_slots: dict[str, str] = field(default_factory=dict)
    context_window: int = 0
    context_input_tokens: int = 0
    usage: dict[str, int] = field(
        default_factory=lambda: {key: 0 for key in USAGE_COUNTER_FIELDS}
    )
    # The client has handed input to the server and is waiting for the message
    # frame that confirms it. It is not a belief about the turn: a queued input
    # stays in flight while the thread is idle.
    submission_in_flight: bool = False
    session_id: str = ""
    thread_id: str = ""
    # The server's own model for the attached thread, as last read. Kept whole
    # rather than field by field: the status report and the read-only rule are
    # both questions about it, and a hand-copied subset would drift.
    thread: ThreadSummary | None = None
    title: str = ""
    agent_name: str = ""
    provider: str = ""
    model: str = ""
    model_mode: str = ""
    _local_counter: int = 0

    @property
    def thread_kind(self) -> str:
        """The wire's word for the attached thread's kind."""
        return self.thread.kind if self.thread is not None else MAIN_THREAD_KIND

    @property
    def read_only(self) -> bool:
        """Whether the attached thread accepts typed input.

        A subagent thread is driven by its parent, not by the user, so the
        composer is read-only while one is on screen. The answer comes from the
        server's thread read, never from a local flag that could drift.
        """
        return self.thread_kind == SUBAGENT_THREAD_KIND

    def next_local_id(self, prefix: str) -> str:
        self._local_counter += 1
        return f"local:{prefix}:{self._local_counter}"

    def window(self, *, size: int, end: str | None = None):
        return self.timeline.window(size=size, end=end)


def reduce(state: SessionState, event: UiEvent, *, now: float | None = None) -> SessionState:
    """Apply one UI event in place and return the same state."""
    clock = time.monotonic() if now is None else now

    if isinstance(event, ConnectionChanged):
        state.facts = replace(state.facts, connection=event.connection)

    elif isinstance(event, SnapshotAdopted):
        _adopt_snapshot(state, event.snapshot)

    elif isinstance(event, SessionConfigured):
        payload = event.payload
        if payload.agent_name:
            state.agent_name = payload.agent_name
        if payload.provider:
            state.provider = payload.provider
        if payload.model:
            state.model = payload.model
        state.model_mode = payload.model_mode or state.model_mode
        state.context_window = payload.context_window or state.context_window

    elif isinstance(event, StatusSlotsUpdated):
        state.status_slots = dict(event.slots)

    elif isinstance(event, ThreadRead):
        turn_status = ServerTurn(event.payload.turn_status)
        state.thread = event.payload
        state.facts = replace(
            state.facts,
            server_turn=turn_status,
            turn_open=turn_status is ServerTurn.RUNNING,
        )
        state.submission_in_flight = False

    elif isinstance(event, UsageUpdated):
        _apply_usage(state, event.payload)

    elif isinstance(event, CompactionChanged):
        _apply_compaction(state, event)

    elif isinstance(event, QueueReplaced):
        state.queue = tuple(event.payload.items)

    elif isinstance(event, ClientNotice):
        _append(state, NoticeEntry(
            id=state.next_local_id("client_message"),
            seq=state.timeline.next_seq(),
            notice_kind="client_message",
            text=event.payload.message,
            level=event.payload.level,
        ))

    elif isinstance(event, TranscriptCleared):
        state.timeline = Timeline()
        state.stream_entry_id = None

    elif isinstance(event, LocalNotice):
        _append(state, NoticeEntry(
            id=state.next_local_id(f"local:{event.notice_kind}"),
            seq=state.timeline.next_seq(),
            notice_kind=event.notice_kind,
            text=event.text,
            level=event.level,
        ))

    elif isinstance(event, JobCompletionNotice):
        payload = event.payload
        text = f"{payload.job_id}: {payload.status}"
        if payload.command:
            text = f"{text} — {payload.command}"
        _append(state, NoticeEntry(
            id=state.next_local_id("completion_notice"),
            seq=state.timeline.next_seq(),
            notice_kind="completion_notice",
            text=text,
        ))

    elif isinstance(event, InteractionOpened):
        state.pending_interactions[_request_id(event.request)] = event.request
        _append(state, NoticeEntry(
            id=_interaction_entry_id(event.request),
            seq=state.timeline.next_seq(),
            notice_kind=f"interaction:{_interaction_kind(event.request)}",
            text=_interaction_text(event.request),
        ))
        _refresh_interaction_facts(state)

    elif isinstance(event, InteractionResolved):
        opened = state.pending_interactions.pop(event.payload.request_id, None)
        if opened is not None:
            entry_id = _interaction_entry_id(opened)
            existing = state.timeline.get(entry_id)
            _append(state, NoticeEntry(
                id=entry_id,
                seq=(
                    existing.seq
                    if isinstance(existing, NoticeEntry)
                    else state.timeline.next_seq()
                ),
                notice_kind=f"interaction:{_interaction_kind(opened)}",
                text=_interaction_text(opened, resolution=event),
            ))
        _refresh_interaction_facts(state)

    elif isinstance(event, StreamGapDetected):
        _append(state, NoticeEntry(
            id=state.next_local_id("gap"),
            seq=state.timeline.next_seq(),
            notice_kind="stream_gap",
            text=(
                f"Missed {event.received - event.expected} event(s) "
                f"(expected {event.expected}, received {event.received}); "
                "the transcript may be incomplete until it is reloaded."
            ),
        ))

    elif isinstance(event, TurnStarted):
        state.turn = event.payload.turn
        state.turn_started_at = clock
        state.facts = replace(
            state.facts,
            server_turn=ServerTurn.RUNNING,
            turn_open=True,
            interrupt=Interrupt.NONE,
            last_error=None,
        )
        state.submission_in_flight = False

    elif isinstance(event, TurnFinished):
        state.turn = event.payload.turn
        _close_stream(state)
        _finalize_open_tools(state, "cancelled", clock)
        state.facts = replace(
            state.facts,
            server_turn=ServerTurn.IDLE,
            turn_open=False,
            interrupt=Interrupt.NONE,
            compaction=False,
        )

    elif isinstance(event, TurnCancelled):
        state.turn = event.payload.turn
        _close_stream(state)
        _finalize_open_tools(state, "cancelled", clock)
        state.facts = replace(
            state.facts,
            server_turn=ServerTurn.IDLE,
            turn_open=False,
            interrupt=Interrupt.NONE,
            compaction=False,
        )
        _append(state, NoticeEntry(
            id=state.next_local_id("cancelled"),
            seq=state.timeline.next_seq(),
            notice_kind="turn_cancelled",
            text=f"Turn interrupted ({event.payload.reason}).",
        ))

    elif isinstance(event, InterruptAsked):
        state.facts = replace(state.facts, interrupt=Interrupt.REQUESTED)

    elif isinstance(event, InterruptSettled):
        if event.cancelled:
            _close_stream(state)
            _finalize_open_tools(state, "cancelled", clock)
            state.facts = replace(
                state.facts,
                interrupt=Interrupt.NONE,
                server_turn=ServerTurn.IDLE,
                turn_open=False,
            )
        else:
            # The server answered "idle": nothing was running. The turn flag is
            # what was wrong here, not the interrupt request, so the request is
            # cleared and the flag is confirmed rather than invented.
            state.facts = replace(
                state.facts,
                interrupt=Interrupt.NONE,
                server_turn=ServerTurn.IDLE,
                turn_open=True,
            )

    elif isinstance(event, AssistantDelta):
        _append_delta(
            state,
            event.payload.content or "",
            event.payload.reasoning or "",
        )

    elif isinstance(event, AssistantCompleted):
        _complete_assistant(state, event.payload.id, event.payload.content)

    elif isinstance(event, HistoryReplaced):
        _rebuild_from_items(state, tuple(event.payload.history))

    elif isinstance(event, ToolCallsStarted):
        _start_tool_calls(state, event.payload.tool_calls, clock)

    elif isinstance(event, ToolResult):
        _finish_tool_call(state, event.payload, clock)

    elif isinstance(event, ErrorFrame):
        message = event.payload.message or event.payload.code
        state.facts = replace(state.facts, last_error=message)
        _finalize_open_tools(state, "error", clock)
        _append(state, ErrorEntry(
            id=state.next_local_id("error"),
            seq=state.timeline.next_seq(),
            message=message,
        ))

    elif isinstance(event, UserInputSubmitted):
        existing = state.timeline.get(event.input_id)
        if not (isinstance(existing, UserEntry) and existing.delivery is Delivery.ACCEPTED):
            # A message the server has already confirmed must not be pushed back
            # to "sending": the echo can arrive before the local bookkeeping.
            _append(state, UserEntry(
                id=event.input_id,
                seq=state.timeline.next_seq(),
                content=event.content,
                delivery=Delivery.PENDING,
            ))
        state.submission_in_flight = True

    elif isinstance(event, UserInputFailed):
        existing = state.timeline.get(event.input_id)
        if isinstance(existing, UserEntry):
            _append(state, replace(existing, delivery=Delivery.FAILED))
        state.submission_in_flight = False
        state.facts = replace(state.facts, last_error=event.error)
        _append(state, ErrorEntry(
            id=state.next_local_id("submit_error"),
            seq=state.timeline.next_seq(),
            message=f"Message not delivered: {event.error}",
        ))

    elif isinstance(event, UserMessagePublished):
        _publish_user_message(state, event)

    elif isinstance(event, JobUpdated):
        state.jobs[event.payload.job_id] = event.payload
        state.facts = replace(
            state.facts,
            jobs_running=sum(
                1
                for job in state.jobs.values()
                if job.status in _RUNNING_JOB_STATUSES
            ),
        )

    else:  # pragma: no cover - the union is closed
        raise TypeError(f"Unsupported UI event: {event!r}")

    return state


# --- usage and compaction -------------------------------------------------


def _apply_usage(state: SessionState, usage: UsageData) -> None:
    state.context_input_tokens = _effective_context_tokens(
        usage, state.context_input_tokens
    )
    for key in USAGE_COUNTER_FIELDS:
        if key in usage.model_fields_set:
            state.usage[key] += int(getattr(usage, key))


def _apply_compaction(state: SessionState, event: CompactionChanged) -> None:
    payload = event.payload
    if isinstance(payload, CompactionStartedData):
        state.facts = replace(state.facts, compaction=True)
        return
    state.facts = replace(state.facts, compaction=False)
    if isinstance(payload, CompactionFailedData):
        _append(state, NoticeEntry(
            id=state.next_local_id("compaction_failed"),
            seq=state.timeline.next_seq(),
            notice_kind="compaction",
            text=f"Compaction failed: {payload.message}",
        ))
        return
    if not isinstance(payload, CompactionCompletedData):  # pragma: no cover - union
        raise TypeError(f"Unsupported compaction payload: {payload!r}")
    _append(state, NoticeEntry(
        id=state.next_local_id("compaction"),
        seq=state.timeline.next_seq(),
        notice_kind="compaction",
        text="Conversation compacted",
        detail=payload.summary,
    ))


def _effective_context_tokens(usage: UsageData, previous: int) -> int:
    """Context size for the status bar.

    ``context_tokens`` is authoritative when the provider reported it; providers
    that only report the input breakdown are summed; an event that carries
    neither leaves the previous reading alone.
    """
    if "context_tokens" in usage.model_fields_set:
        return int(usage.context_tokens)
    if any(field in usage.model_fields_set for field in INPUT_USAGE_FIELDS):
        return sum(int(getattr(usage, field)) for field in INPUT_USAGE_FIELDS)
    return previous


# --- timeline mutation ----------------------------------------------------


def _append(state: SessionState, entry: Entry) -> None:
    state.timeline.upsert(entry)


def _append_delta(state: SessionState, content: str, reasoning: str) -> None:
    if not content and not reasoning:
        return
    entry_id = state.stream_entry_id
    existing = state.timeline.get(entry_id) if entry_id is not None else None
    if not isinstance(existing, AssistantEntry):
        entry_id = state.next_local_id("assistant")
        state.stream_entry_id = entry_id
        existing = AssistantEntry(id=entry_id, seq=state.timeline.next_seq())
    _append(state, replace(
        existing,
        content=existing.content + content,
        reasoning=existing.reasoning + reasoning,
        streaming=True,
    ))


def _complete_assistant(state: SessionState, message_id: str, content: str) -> None:
    stream_id = state.stream_entry_id
    streamed = state.timeline.get(stream_id) if stream_id is not None else None
    if isinstance(streamed, AssistantEntry):
        # The frame carries the authoritative body; the accumulated reasoning
        # stays, because only the delta frames ever carried it.
        _append(state, replace(
            streamed,
            content=content or streamed.content,
            streaming=False,
        ))
        state.stream_entry_id = None
        return
    _append(state, AssistantEntry(
        id=message_id or state.next_local_id("assistant"),
        seq=state.timeline.next_seq(),
        content=content,
    ))


def _close_stream(state: SessionState) -> None:
    """Commit the streaming suffix without moving it."""
    stream_id = state.stream_entry_id
    if stream_id is None:
        return
    streamed = state.timeline.get(stream_id)
    if isinstance(streamed, AssistantEntry) and streamed.streaming:
        _append(state, replace(streamed, streaming=False))
    state.stream_entry_id = None


def _finalize_open_tools(state: SessionState, status: ToolStatus, now: float) -> None:
    for entry in list(state.timeline):
        if isinstance(entry, ToolEntry) and entry.status in _UNFINISHED_TOOL_STATUSES:
            _append(state, replace(entry, status=status, finished_at=now))


def _start_tool_calls(state: SessionState, calls, now: float) -> None:
    for call in calls:
        existing = state.timeline.get(call.id)
        if isinstance(existing, ToolEntry):
            _append(state, replace(
                existing,
                name=call.name or existing.name,
                args=dict(call.args) or existing.args,
            ))
            continue
        _append(state, ToolEntry(
            id=call.id,
            seq=state.timeline.next_seq(),
            name=call.name,
            args=dict(call.args),
            status="running",
            started_at=now,
        ))


def _finish_tool_call(state: SessionState, payload, now: float) -> None:
    existing = state.timeline.get(payload.tool_call_id)
    known = isinstance(existing, ToolEntry)
    body = payload.content if payload.error is None else payload.error
    _append(state, ToolEntry(
        id=payload.tool_call_id,
        seq=existing.seq if known else state.timeline.next_seq(),
        name=payload.name or (existing.name if known else "tool"),
        args=existing.args if known else {},
        status=payload.status,
        result=body,
        started_at=existing.started_at if known else now,
        finished_at=now,
    ))


def _publish_user_message(state: SessionState, event: UserMessagePublished) -> None:
    # I6: the streaming suffix is committed before the interjection lands, so a
    # later delta opens a new entry below the user's message instead of growing
    # the entry above it.
    _close_stream(state)
    message_id = event.payload.id
    existing = state.timeline.get(message_id)
    if isinstance(existing, UserEntry):
        _append(state, replace(existing, delivery=Delivery.ACCEPTED))
    else:
        _append(state, UserEntry(
            id=message_id,
            seq=state.timeline.next_seq(),
            content=event.payload.content,
            delivery=Delivery.ACCEPTED,
        ))
    state.submission_in_flight = False


# --- baseline adoption ----------------------------------------------------


def _adopt_snapshot(state: SessionState, snapshot: OpenSessionResponse) -> None:
    """Adopt a session descriptor as the new baseline.

    The descriptor carries no turn answer, so this deliberately leaves
    ``server_turn``/``turn_open`` alone: an answerless snapshot is not evidence
    that nothing is running.
    """
    state.session_id = snapshot.session_id or state.session_id
    state.thread_id = snapshot.thread_id or state.thread_id
    state.title = snapshot.title or state.title
    state.agent_name = snapshot.agent_name or state.agent_name
    state.provider = snapshot.provider or state.provider
    state.model = snapshot.model or state.model
    state.model_mode = snapshot.model_mode or state.model_mode
    state.context_window = snapshot.context_window or state.context_window
    state.status_slots = dict(snapshot.status_slots)
    state.usage = {
        key: int(getattr(snapshot.usage, key, 0) or 0)
        for key in USAGE_COUNTER_FIELDS
    }
    state.context_input_tokens = _effective_context_tokens(
        snapshot.usage, state.context_input_tokens
    )
    state.queue = tuple(snapshot.pending_inputs)
    # Unanswered prompts arrive as ``pending_interactions``; the transport
    # replays each one as an ``InteractionOpened`` event so a reconnecting
    # client rebuilds the dialog through the same path as a live one.
    _rebuild_from_items(state, tuple(snapshot.history))


def _rebuild_from_items(
    state: SessionState,
    items: tuple[SessionHistoryItem, ...],
) -> None:
    """Replace the timeline with a server-authored baseline.

    A snapshot or an explicit history rewrite is a fresh start, not a window
    move: entries the client had are superseded, and any open stream target is
    dropped so a later delta cannot be appended into a stale entry.
    """
    state.timeline = Timeline()
    state.stream_entry_id = None
    for item in items:
        state.timeline.upsert(_entry_from_history_item(state, item))


def _entry_from_history_item(state: SessionState, item: SessionHistoryItem) -> Entry:
    seq = state.timeline.next_seq()
    if item.role == "user":
        if item.runtime is not None:
            # An injected reminder or goal round carries runtime provenance and
            # is not something the human typed.
            return NoticeEntry(
                id=state.next_local_id("runtime"),
                seq=seq,
                notice_kind="runtime",
                text=item.content or "injected turn",
            )
        return UserEntry(
            id=state.next_local_id("user"),
            seq=seq,
            content=item.content,
            delivery=Delivery.ACCEPTED,
        )
    if item.role == "assistant":
        return AssistantEntry(
            id=state.next_local_id("assistant"),
            seq=seq,
            content=item.content,
            reasoning=item.reasoning,
        )
    if item.role == "tool":
        # A persisted tool record has finished, and its status already speaks the
        # same vocabulary as a live result.
        return ToolEntry(
            id=item.tool_call_id or state.next_local_id("tool"),
            seq=seq,
            name="tool",
            status=item.status or "success",
            result=item.content,
        )
    raise ValueError(f"Unsupported history role: {item.role!r}")


# --- interactions ---------------------------------------------------------


def _request_id(request: InteractionRequest) -> str:
    return str(request.request_id)


def _interaction_entry_id(request: InteractionRequest) -> str:
    return f"interaction:{request.request_id}"


def _interaction_kind(request: InteractionRequest) -> str:
    if isinstance(request, PermissionRequestData):
        return "permission"
    if isinstance(request, UserInputRequiredData):
        return "user_input"
    raise TypeError(f"Unsupported interaction request: {request!r}")


def _interaction_text(
    request: InteractionRequest,
    resolution: InteractionResolved | None = None,
) -> str:
    if isinstance(request, PermissionRequestData):
        subject = (
            request.tool_call.name
            if request.tool_call is not None
            else str(getattr(request.permission, "tool", "") or "tool")
        )
        lines = [f"{subject}: {request.reason}"]
    elif isinstance(request, UserInputRequiredData):
        lines = [request.question]
        lines.extend(
            f"{index}. {option.label} — {option.description}"
            for index, option in enumerate(request.options, start=1)
        )
    else:
        raise TypeError(f"Unsupported interaction request: {request!r}")
    if resolution is not None:
        lines.append(f"→ {resolution.payload.decision or resolution.payload.status}")
    return "\n".join(lines)


def _refresh_interaction_facts(state: SessionState) -> None:
    """Derive the blocking-prompt fact from the prompts still unanswered."""
    kinds = {_interaction_kind(request) for request in state.pending_interactions.values()}
    if "permission" in kinds:
        interaction = Interaction.PERMISSION
    elif "user_input" in kinds:
        interaction = Interaction.USER_INPUT
    else:
        interaction = Interaction.NONE
    state.facts = replace(state.facts, interaction=interaction)


__all__ = ["SessionState", "reduce"]
