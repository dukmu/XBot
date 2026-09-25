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
from enum import Enum
from typing import Mapping

from XBotv2.compact.protocol import (
    CompactionCompleted,
    CompactionFailed,
    CompactionStarted,
)
from XBotv2.agentloop.protocol import (
    AssistantReasoningDelta,
    TurnCancelled as CancelledOutcome,
)
from XBotv2.core.domain import (
    Cursor,
    ReasoningGenerationMode,
    ResolvedRuntimeSelection,
    UsageSnapshot,
)
from XBotv2.interactions.protocol import UserInputRecorded, UserInputRequest
from XBotv2.goal.models import ActiveGoal, NoGoal
from XBotv2.jobs.contracts import JobView
from XBotv2.permissions.contracts import NamedPermission, PermissionRequest, ToolPermission
from XBotv2.permissions.protocol import PermissionResponseRecorded
from XBotv2.session.contracts import PendingInputData
from XBotv2.session.records import (
    AssistantRecord,
    CompactionSummaryRecord,
    ConversationRecord,
    HumanInputRecord,
    RuntimeNoticeRecord,
    ToolRecord,
)
from XBotv2.core.parts import TextPart
from XBotv2.core.tools import (
    ToolCancelled,
    ToolDenied,
    ToolFailed,
    ToolSucceeded,
)
from XBotv2.session.protocol import OpenSessionResponse
from XBotv2.tui.events import (
    AssistantCompleted,
    AssistantDelta,
    ClientNoticeReceived,
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
    GoalChangedReceived,
    TaskChangedReceived,
    JobUpdated,
    LocalNotice,
    OlderHistoryFailed,
    OlderHistoryLoaded,
    OlderHistoryRequested,
    TranscriptCleared,
    QueueReplaced,
    RuntimeNoticePublished,
    SessionConfigured,
    SnapshotAdopted,
    StatusSlotsUpdated,
    StreamGapDetected,
    ToolCallsStarted,
    ToolRecordReceived,
    TurnCancelled,
    TurnFinished,
    TurnStarted,
    UiEvent,
    UsageSnapshotReceived,
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
_RUNNING_JOB_STATUSES: frozenset[str] = frozenset({"queued", "running"})


# ``ThreadSummary.kind`` is the authority for these two words
# (``tests/tui/test_reuse.py`` pins them to the wire's Literal).
MAIN_THREAD_KIND = "main"
SUBAGENT_THREAD_KIND = "subagent"

@dataclass
class HeldPage:
    """One page the reader loaded, and the cursor that preceded it.

    The pair is what makes releasing safe: a page is exactly the span between
    those two cursors, so putting the first one back names the page before the
    new front, and the reader can load the same page again.
    """

    cursor: Cursor
    ids: tuple[str, ...]


@dataclass(frozen=True)
class HistoryComplete:
    """The client holds the beginning of the conversation."""


@dataclass(frozen=True)
class HistoryKnown:
    """The states that name the page before the oldest entry held.

    ``cursor`` is the server's own cursor for that page. The three subclasses are
    the only states in which a page can be asked for, retried, or awaited, so a
    request never has to ask whether a cursor exists.
    """

    cursor: Cursor


@dataclass(frozen=True)
class HistoryAvailable(HistoryKnown):
    """Older messages exist and can be loaded."""


@dataclass(frozen=True)
class HistoryLoading(HistoryKnown):
    """That page is being read right now."""


@dataclass(frozen=True)
class HistoryFailed(HistoryKnown):
    """The page could not be read; the cursor is kept so the reader can retry."""

    message: str


#: What the client knows about the conversation before its oldest entry, as one
#: value. The cursor exists only in the variants that can fetch a page, so there
#: is no state in which a missing cursor has to be interpreted.
OlderHistory = HistoryComplete | HistoryAvailable | HistoryLoading | HistoryFailed


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
    jobs: dict[str, JobView] = field(default_factory=dict)
    # Unanswered prompts by request id. ``facts.interaction`` is derived from
    # this, so several concurrent prompts cannot lose one another.
    pending_interactions: dict[str, InteractionRequest] = field(default_factory=dict)
    status_slots: dict[str, str] = field(default_factory=dict)
    runtime_selection: ResolvedRuntimeSelection | None = None
    usage: UsageSnapshot = field(default_factory=UsageSnapshot)
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
    # Everything the client knows about the part of the conversation it does not
    # hold, as one value: whether it holds the beginning, where the page before
    # its oldest entry starts, whether one is in flight, and why the last one
    # failed. There is no cursor field to fall back to and none to contradict it.
    older: OlderHistory = field(default_factory=HistoryComplete)
    # Pages the reader asked for, oldest last: the front-most page is the one
    # most recently loaded, and therefore the one released first.
    loaded_pages: list[HeldPage] = field(default_factory=list)
    _local_counter: int = 0



    @property
    def thread_kind(self) -> str:
        """The wire's word for the attached thread's kind."""
        return self.thread.kind if self.thread is not None else MAIN_THREAD_KIND

    @property
    def agent_name(self) -> str:
        return self.runtime_selection.agent_name if self.runtime_selection else ""

    @property
    def provider(self) -> str:
        return self.runtime_selection.model.route.provider if self.runtime_selection else ""

    @property
    def model(self) -> str:
        return self.runtime_selection.model.route.model if self.runtime_selection else ""

    @property
    def model_mode(self) -> str:
        if self.runtime_selection is None:
            return ""
        mode = self.runtime_selection.model.generation.mode
        return mode.effort if isinstance(mode, ReasoningGenerationMode) else ""

    @property
    def context_window(self) -> int:
        return self.runtime_selection.model.context_window if self.runtime_selection else 0

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
        state.runtime_selection = event.payload.runtime_selection

    elif isinstance(event, StatusSlotsUpdated):
        state.status_slots = dict(event.slots)

    elif isinstance(event, ThreadRead):
        turn_status = ServerTurn(event.payload.turn_status)
        state.thread = event.payload
        state.title = event.payload.title
        # A fresh client has not seen the historical turn frames.  The thread
        # read already carries the count derived from the canonical persisted
        # conversation, so it is also the authority for the displayed turn
        # number after attach, resume, undo, or compaction.
        state.turn = event.payload.session_stats.turns
        state.facts = replace(
            state.facts,
            server_turn=turn_status,
            turn_open=turn_status is ServerTurn.RUNNING,
        )
        if turn_status is not ServerTurn.RUNNING:
            # The server says this thread is not running, so anything still open
            # belongs to a turn that ended -- most likely a terminal frame that
            # was lost. Leaving it running would show a tool that never finishes.
            _close_stream(state)
            _finalize_open_tools(state, "cancelled", clock)
        state.submission_in_flight = False

    elif isinstance(event, UsageSnapshotReceived):
        state.usage = event.payload.snapshot

    elif isinstance(event, CompactionChanged):
        _apply_compaction(state, event)

    elif isinstance(event, QueueReplaced):
        state.queue = tuple(event.payload.items)

    elif isinstance(event, ClientNoticeReceived):
        _append(state, NoticeEntry(
            id=state.next_local_id("client_message"),
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
            notice_kind=event.notice_kind,
            text=event.text,
            level=event.level,
        ))

    elif isinstance(event, JobCompletionNotice):
        payload = event.payload
        text = f"{payload.view.id}: {payload.view.state}"
        if payload.view.label:
            text = f"{text} — {payload.view.label}"
        _append(state, NoticeEntry(
            id=state.next_local_id("completion_notice"),
            notice_kind="completion_notice",
            text=text,
        ))

    elif isinstance(event, GoalChangedReceived):
        goal = event.payload.snapshot
        state_value = goal.state
        if isinstance(state_value, NoGoal):
            text = "Goal cleared"
            detail = ""
        else:
            text = f"Goal {state_value.kind}: {state_value.condition}"
            detail = "" if isinstance(state_value, ActiveGoal) else state_value.reason
        _append(state, NoticeEntry(
            id="goal:active",
            notice_kind="goal",
            text=text,
            detail=detail,
        ))

    elif isinstance(event, TaskChangedReceived):
        tasks = event.payload.snapshot.tasks
        completed = sum(task.status == "completed" for task in tasks)
        _append(state, NoticeEntry(
            id="tasks:active",
            notice_kind="tasks",
            text=f"Tasks {completed}/{len(tasks)} completed",
        ))

    elif isinstance(event, InteractionOpened):
        state.pending_interactions[_request_id(event.request)] = event.request
        _append(state, NoticeEntry(
            id=_interaction_entry_id(event.request),
            notice_kind=f"interaction:{_interaction_kind(event.request)}",
            text=_interaction_text(event.request),
        ))
        _refresh_interaction_facts(state)

    elif isinstance(event, InteractionResolved):
        opened = state.pending_interactions.pop(event.payload.interaction_id, None)
        if opened is not None:
            entry_id = _interaction_entry_id(opened)
            existing = state.timeline.get(entry_id)
            _append(state, NoticeEntry(
                id=entry_id,
                notice_kind=f"interaction:{_interaction_kind(opened)}",
                text=_interaction_text(opened, resolution=event),
            ))
        _refresh_interaction_facts(state)

    elif isinstance(event, StreamGapDetected):
        _append(state, NoticeEntry(
            id=state.next_local_id("gap"),
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
            notice_kind="turn_cancelled",
            text=(
                "Turn interrupted "
                f"({event.payload.outcome.reason})."
                if isinstance(event.payload.outcome, CancelledOutcome)
                else "Turn interrupted."
            ),
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
            # The server answered "idle": nothing was running, so the local turn
            # flag is stale. Confirming it instead of adopting the answer kept
            # the status on "Running" (derive() trusts turn_open) and left the
            # turn's tools running for good -- ported from the runtime-compat
            # work on fix-runtime-stream-context-compat.
            _close_stream(state)
            _finalize_open_tools(state, "cancelled", clock)
            state.facts = replace(
                state.facts,
                interrupt=Interrupt.NONE,
                server_turn=ServerTurn.IDLE,
                turn_open=False,
            )

    elif isinstance(event, AssistantDelta):
        if isinstance(event.payload, AssistantReasoningDelta):
            _append_delta(state, "", event.payload.text)
        else:
            _append_delta(state, event.payload.text, "")

    elif isinstance(event, AssistantCompleted):
        _complete_assistant(state, event.payload)

    elif isinstance(event, HistoryReplaced):
        mutation = event.payload.mutation
        _rebuild_from_items(state, mutation.history.items)
        state.older = _older_from_cursor(mutation.history.older_cursor)
        state.turn = mutation.stats.turns
        state.loaded_pages.clear()

    elif isinstance(event, OlderHistoryRequested):
        # An ask while a page is already in flight changes nothing: that page is
        # the answer to this one too. An ask after a failure is a retry.
        if isinstance(state.older, HistoryKnown):
            state.older = HistoryLoading(cursor=state.older.cursor)

    elif isinstance(event, OlderHistoryLoaded):
        added = _prepend_from_items(state, event.payload.items)
        state.loaded_pages.append(HeldPage(cursor=event.cursor, ids=added))
        state.older = _older_from_cursor(event.payload.older_cursor)

    elif isinstance(event, OlderHistoryFailed):
        if isinstance(state.older, HistoryLoading):
            state.older = HistoryFailed(
                cursor=state.older.cursor, message=event.message,
            )

    elif isinstance(event, ToolCallsStarted):
        _start_tool_calls(
            state,
            tuple(item.call for item in event.payload.calls),
            clock,
        )

    elif isinstance(event, ToolRecordReceived):
        _finish_tool_call(state, event.payload, clock)

    elif isinstance(event, ErrorFrame):
        message = event.payload.message or event.payload.code
        state.facts = replace(state.facts, last_error=message)
        _finalize_open_tools(state, "error", clock)
        _append(state, ErrorEntry(
            id=state.next_local_id("error"),
            message=message,
        ))

    elif isinstance(event, UserInputSubmitted):
        existing = state.timeline.get(event.input_id)
        if not (isinstance(existing, UserEntry) and existing.delivery is Delivery.ACCEPTED):
            # A message the server has already confirmed must not be pushed back
            # to "sending": the echo can arrive before the local bookkeeping.
            _append(state, UserEntry(
                id=event.input_id,
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
            message=f"Message not delivered: {event.error}",
        ))

    elif isinstance(event, UserMessagePublished):
        _publish_user_message(state, event)

    elif isinstance(event, RuntimeNoticePublished):
        _append(state, _entry_from_history_item(state, event.payload))

    elif isinstance(event, JobUpdated):
        state.jobs[event.payload.id] = event.payload
        state.facts = replace(
            state.facts,
            jobs_running=sum(
                1
                for job in state.jobs.values()
                if job.state in _RUNNING_JOB_STATUSES
            ),
        )

    else:  # pragma: no cover - the union is closed
        raise TypeError(f"Unsupported UI event: {event!r}")

    return state


# --- usage and compaction -------------------------------------------------


def _apply_compaction(state: SessionState, event: CompactionChanged) -> None:
    payload = event.payload
    if isinstance(payload, CompactionStarted):
        state.facts = replace(state.facts, compaction=True)
        return
    state.facts = replace(state.facts, compaction=False)
    if isinstance(payload, CompactionFailed):
        _append(state, NoticeEntry(
            id=state.next_local_id("compaction_failed"),
            notice_kind="compaction",
            text=f"Compaction failed: {payload.message}",
        ))
        return
    if not isinstance(payload, CompactionCompleted):  # pragma: no cover - union
        raise TypeError(f"Unsupported compaction payload: {payload!r}")
    _append(state, NoticeEntry(
        id=state.next_local_id("compaction"),
        notice_kind="compaction",
        text="Conversation compacted",
        detail=payload.summary.summary,
    ))


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
        existing = AssistantEntry(id=entry_id)
    _append(state, replace(
        existing,
        content=existing.content + content,
        reasoning=existing.reasoning + reasoning,
        streaming=True,
    ))


def _complete_assistant(state: SessionState, record: AssistantRecord) -> None:
    stream_id = state.stream_entry_id
    streamed = state.timeline.get(stream_id) if stream_id is not None else None
    if isinstance(streamed, AssistantEntry):
        # Delta entries use a local identity; the completed record owns the
        # durable identity. Replace the streaming placeholder rather than
        # retaining both as separate transcript entries.
        if stream_id != record.id:
            state.timeline.remove(stream_id)
        _append(state, replace(
            streamed,
            id=record.id,
            content=record.content,
            reasoning=record.reasoning,
            streaming=False,
        ))
        state.stream_entry_id = None
        return
    _append(state, AssistantEntry(
        id=record.id,
        content=record.content,
        reasoning=record.reasoning,
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
            name=call.name,
            args=dict(call.args),
            status="running",
            started_at=now,
        ))


def _finish_tool_call(state: SessionState, record: ToolRecord, now: float) -> None:
    call_id = str(record.call.id)
    existing = state.timeline.get(call_id)
    known = isinstance(existing, ToolEntry)
    outcome = record.outcome
    if isinstance(outcome, ToolSucceeded):
        status: ToolStatus = "success"
        body = _tool_output_text(outcome.output.parts)
    elif isinstance(outcome, ToolFailed):
        status = "error"
        body = _tool_output_text(outcome.output.parts) or outcome.error.message
    elif isinstance(outcome, ToolDenied):
        status = "denied"
        body = outcome.reason
    elif isinstance(outcome, ToolCancelled):
        status = "cancelled"
        body = outcome.reason
    else:  # pragma: no cover - ToolOutcome is closed
        raise TypeError(f"Unsupported tool outcome: {type(outcome).__name__}")
    if call_id != record.id:
        state.timeline.remove(call_id)
    _append(state, ToolEntry(
        id=record.id,
        name=record.call.name,
        args=existing.args if known else {},
        status=status,
        result=body,
        started_at=existing.started_at if known else now,
        finished_at=now,
    ))


def _tool_output_text(parts: tuple[object, ...]) -> str:
    return "".join(part.text for part in parts if isinstance(part, TextPart))


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
    opened = snapshot.data
    metadata = opened.metadata
    state.session_id = opened.key.session_id
    state.thread_id = opened.key.thread_id
    state.title = metadata.title
    state.runtime_selection = metadata.runtime_selection
    state.status_slots = dict(opened.status_slots)
    state.usage = opened.usage
    state.queue = tuple(opened.pending_inputs)
    # Unanswered prompts arrive as ``pending_interactions``; the transport
    # replays each one as an ``InteractionOpened`` event so a reconnecting
    # client rebuilds the dialog through the same path as a live one.
    _rebuild_from_items(state, opened.history.items)
    state.older = _older_from_cursor(opened.history.older_cursor)
    state.loaded_pages.clear()


def _rebuild_from_items(
    state: SessionState,
    items: tuple[ConversationRecord, ...],
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


def _prepend_from_items(
    state: SessionState,
    items: tuple[ConversationRecord, ...],
) -> tuple[str, ...]:
    """Add one older page ahead of what the client holds; return the new ids.

    A page is older than the window it extends, so nothing the client already
    holds moves; an id that appears in both is refreshed where it stands and is
    not part of what this page added.
    """
    return state.timeline.prepend(
        [_entry_from_history_item(state, item) for item in items]
    )


def _older_from_cursor(cursor: Cursor | None) -> OlderHistory:
    """What a server cursor means about the conversation before the window.

    A cursor names the page before the oldest entry held; the server sends none
    exactly when that entry is the beginning of the conversation.
    """
    if cursor is None:
        return HistoryComplete()
    return HistoryAvailable(cursor=cursor)


def release_oldest_loaded_page(state: SessionState) -> None:
    """Let the front-most loaded page go and page back to where it started.

    The caller must have checked that a page is held; asking to release nothing
    is a bug in the caller, not a state the conversation can be in. Only pages
    the *reader* loaded are ever released -- the window the attach returned is
    what the client is for.
    """
    released = state.loaded_pages.pop()
    for entry_id in released.ids:
        state.timeline.remove(entry_id)
    state.older = HistoryAvailable(cursor=released.cursor)


def _entry_from_history_item(state: SessionState, item: ConversationRecord) -> Entry:
    """One replayed item as the entry it names.

    ``item.id`` is the identity of the transcript node the record sits on, so a
    page the client loads later names the same message the same way and upserts
    the entry it already holds.
    """
    entry_id = item.id
    if isinstance(item, RuntimeNoticeRecord):
        return NoticeEntry(
            id=entry_id,
            notice_kind="runtime",
            text=item.content or "injected turn",
        )
    if isinstance(item, HumanInputRecord):
        return UserEntry(
            id=entry_id,
            content=item.content,
            delivery=Delivery.ACCEPTED,
        )
    if isinstance(item, AssistantRecord):
        return AssistantEntry(
            id=entry_id,
            content=item.content,
            reasoning=item.reasoning,
        )
    if isinstance(item, CompactionSummaryRecord):
        return NoticeEntry(
            id=entry_id,
            notice_kind="compaction",
            text=item.summary,
        )
    if isinstance(item, ToolRecord):
        outcome = item.outcome
        result = ""
        if isinstance(outcome, (ToolSucceeded, ToolFailed)):
            result = "".join(
                part.text for part in outcome.output.parts
                if isinstance(part, TextPart)
            )
        return ToolEntry(
            id=entry_id,
            name=item.call.name,
            status={"succeeded": "success", "failed": "error"}.get(
                outcome.kind, outcome.kind
            ),
            result=result,
        )
    raise TypeError(f"Unsupported conversation record: {type(item).__name__}")


# --- interactions ---------------------------------------------------------


def _request_id(request: InteractionRequest) -> str:
    return str(request.interaction_id)


def _interaction_entry_id(request: InteractionRequest) -> str:
    return f"interaction:{request.interaction_id}"


def _interaction_kind(request: InteractionRequest) -> str:
    if isinstance(request, PermissionRequest):
        return "permission"
    if isinstance(request, UserInputRequest):
        return "user_input"
    raise TypeError(f"Unsupported interaction request: {request!r}")


def _interaction_text(
    request: InteractionRequest,
    resolution: InteractionResolved | None = None,
) -> str:
    lines = [f"ID: {request.interaction_id}"]
    if isinstance(request, PermissionRequest):
        if isinstance(request.subject, ToolPermission):
            subject = request.subject.tool_call.name
        elif isinstance(request.subject, NamedPermission):
            subject = request.subject.tool
        else:  # pragma: no cover - PermissionSubject is closed
            raise TypeError(f"Unsupported permission subject: {request.subject!r}")
        lines.append(f"{subject}: {request.reason}")
    elif isinstance(request, UserInputRequest):
        lines.append(request.question)
        lines.extend(
            f"{index}. {option.label} — {option.description}"
            for index, option in enumerate(request.options, start=1)
        )
    else:
        raise TypeError(f"Unsupported interaction request: {request!r}")
    if resolution is not None:
        payload = resolution.payload
        if isinstance(payload, PermissionResponseRecorded):
            result_kind = payload.approval.kind
        elif isinstance(payload, UserInputRecorded):
            result_kind = payload.resolution.kind
        else:  # pragma: no cover - InteractionResolved is closed
            raise TypeError(f"Unsupported interaction resolution: {payload!r}")
        lines.append(f"→ {result_kind}")
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
