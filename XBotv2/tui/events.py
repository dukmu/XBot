"""Typed inputs to the session reducer.

Everything that can change the UI arrives here first: server frames (translated
by ``protocol.py``), local submissions, interrupt results, watchdog readings.

Two rules keep this module from re-declaring the protocol:

* a frame-shaped event **carries the producer's payload model** (loop events,
  ``AssistantRecord``, ``ToolRecord``, ``CompactionCompleted``, ...)
  instead of restating its fields. The wire shape is defined once, by its owner.
* only genuinely client-side events declare their own fields: a submission, an
  interrupt request, a watchdog reading, a gap, a connection change.

Nothing here imports Textual or performs IO, so the whole reducer is testable in
isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Union

from XBotv2.agentloop.protocol import (
    AssistantReasoningDelta,
    AssistantTextDelta,
    LoopError,
    LoopTurnEnded,
    LoopTurnStarted,
    StartedToolCall,
    ToolCallsStarted as LoopToolCallsStarted,
)
from XBotv2.compact.protocol import (
    CompactionCompleted,
    CompactionFailed,
    CompactionStarted,
)
from XBotv2.usage import UsageUpdated
from XBotv2.interactions.protocol import (
    ClientNotice,
    UserInputRequest,
    UserInputRecorded,
)
from XBotv2.jobs.contracts import JobView
from XBotv2.jobs.protocol import JobCompletedEvent, JobListResponse
from XBotv2.goal.models import GoalChanged
from XBotv2.todolist.contracts import TaskChanged
from XBotv2.session.contracts import ThreadSummary
from XBotv2.core.domain import Cursor
from XBotv2.core.history import HistoryPage
from XBotv2.permissions.contracts import PermissionRequest
from XBotv2.permissions.protocol import PermissionResponseRecorded
from XBotv2.session.protocol import (
    AgentConfiguredData,
    HistoryUpdatedEvent,
    OpenSessionResponse,
    QueueUpdatedData,
)
from XBotv2.session.records import (
    AssistantRecord,
    HumanInputRecord,
    RuntimeNoticeRecord,
    ToolRecord,
    ConversationRecord,
)
from XBotv2.tui.status import Connection, ServerTurn

InteractionRequest = Union[PermissionRequest, UserInputRequest]
CompactionPayload = Union[
    CompactionStarted, CompactionCompleted, CompactionFailed
]


# --- transport and session lifecycle --------------------------------------


@dataclass(frozen=True)
class ConnectionChanged:
    connection: Connection
    reason: str = ""


@dataclass(frozen=True)
class SnapshotAdopted:
    """The session descriptor, adopted as a fresh baseline.

    The descriptor carries no turn answer, so adopting one never changes what
    the client believes about a running turn. Turn facts come from the turn
    frames and from ``WatchdogRead``.
    """

    snapshot: OpenSessionResponse


@dataclass(frozen=True)
class JobsReplaced:
    """Authoritative jobs returned for the newly attached thread."""

    payload: JobListResponse


@dataclass(frozen=True)
class SessionConfigured:
    payload: AgentConfiguredData


@dataclass(frozen=True)
class StatusSlotsUpdated:
    """Status slots travel beside the turn frames that carry them."""

    slots: Mapping[str, str] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class ThreadRead:
    """An authoritative read of the attached thread.

    It carries the server's own thread model rather than a restatement of it, so
    the turn status *and* the thread's kind (main or subagent) come from the same
    authoritative answer.
    """

    payload: ThreadSummary


@dataclass(frozen=True)
class StreamGapDetected:
    """The frame sequence skipped; the user must be told."""

    expected: int = 0
    received: int = 0


# --- turn lifecycle -------------------------------------------------------


@dataclass(frozen=True)
class TurnStarted:
    payload: LoopTurnStarted


@dataclass(frozen=True)
class TurnFinished:
    payload: LoopTurnEnded


@dataclass(frozen=True)
class TurnCancelled:
    payload: LoopTurnEnded


@dataclass(frozen=True)
class InterruptAsked:
    pass


@dataclass(frozen=True)
class InterruptSettled:
    cancelled: bool = False


# --- conversation content -------------------------------------------------


@dataclass(frozen=True)
class AssistantDelta:
    payload: AssistantTextDelta | AssistantReasoningDelta


@dataclass(frozen=True)
class AssistantCompleted:
    payload: AssistantRecord


@dataclass(frozen=True)
class HistoryReplaced:
    payload: HistoryUpdatedEvent


@dataclass(frozen=True)
class ToolCallsStarted:
    payload: LoopToolCallsStarted


@dataclass(frozen=True)
class ToolRecordReceived:
    payload: ToolRecord


@dataclass(frozen=True)
class ErrorFrame:
    payload: LoopError


@dataclass(frozen=True)
class ClientNoticeReceived:
    """A server-authored line for the transcript (``client_message``)."""

    payload: ClientNotice


@dataclass(frozen=True)
class TranscriptCleared:
    """The user asked to stop looking at what is already rendered.

    It clears the client's window, not the conversation: durable history lives on
    the server, and the next snapshot is authoritative again.
    """


@dataclass(frozen=True)
class LocalNotice:
    """Something the client itself needs to tell the user.

    Not a server frame: an unknown command, a copy result, a help listing. It
    becomes a transcript entry like any other, so it cannot be lost.
    """

    notice_kind: str = "info"
    text: str = ""
    level: str = "info"


@dataclass(frozen=True)
class JobCompletionNotice:
    """A background job finished.

    Deliberately a notice and not a turn event: a completion must never open or
    extend a turn.
    """

    payload: JobCompletedEvent


@dataclass(frozen=True)
class GoalChangedReceived:
    payload: GoalChanged


@dataclass(frozen=True)
class TaskChangedReceived:
    payload: TaskChanged


# --- usage, compaction, queue --------------------------------------------


@dataclass(frozen=True)
class UsageSnapshotReceived:
    payload: UsageUpdated


@dataclass(frozen=True)
class CompactionChanged:
    """One compaction transition; the active state is derived from the payload."""

    payload: CompactionPayload


@dataclass(frozen=True)
class QueueReplaced:
    payload: QueueUpdatedData


# --- interactions ---------------------------------------------------------


@dataclass(frozen=True)
class InteractionOpened:
    """An unanswered prompt that blocks the turn."""

    request: InteractionRequest


@dataclass(frozen=True)
class InteractionResolved:
    payload: PermissionResponseRecorded | UserInputRecorded


# --- local input ----------------------------------------------------------


@dataclass(frozen=True)
class UserInputSubmitted:
    input_id: str
    content: str


@dataclass(frozen=True)
class UserInputFailed:
    input_id: str
    error: str


@dataclass(frozen=True)
class UserMessagePublished:
    """The server accepted a user message.

    ``UserEntry.delivery`` is the client's view of where that message stands;
    whether the inbox targeted the next step or the next turn is *queue* state
    and arrives through ``QueueReplaced``, not through this frame.
    """

    payload: HumanInputRecord


@dataclass(frozen=True)
class RuntimeNoticePublished:
    payload: RuntimeNoticeRecord


# --- older history --------------------------------------------------------


@dataclass(frozen=True)
class OlderHistoryRequested:
    """The reader asked for the page before the oldest entries held.

    The client asks exactly once per page: it stays pending until the page
    arrives or fails, so holding the key down is one request, not a stream.
    """


@dataclass(frozen=True)
class OlderHistoryLoaded:
    """One older page arrived.

    ``cursor`` is the request cursor that produced it: the page spans exactly the
    entries between that cursor and the one in the payload, which is what lets
    the client release the page again and stand where it stood before.
    ``payload.older_cursor`` is the cursor for the page before this one, or None
    when the client now holds the beginning of the conversation.
    """

    payload: HistoryPage[ConversationRecord]
    cursor: Cursor


@dataclass(frozen=True)
class OlderHistoryFailed:
    """The page could not be read. Kept visible so the reader can retry."""

    message: str


# --- background work ------------------------------------------------------


@dataclass(frozen=True)
class JobUpdated:
    """One job snapshot. The server publishes jobs one at a time."""

    payload: JobView


UiEvent = Union[
    ConnectionChanged,
    SnapshotAdopted,
    SessionConfigured,
    StatusSlotsUpdated,
    ThreadRead,
    StreamGapDetected,
    TurnStarted,
    TurnFinished,
    TurnCancelled,
    InterruptAsked,
    InterruptSettled,
    AssistantDelta,
    AssistantCompleted,
    HistoryReplaced,
    ToolCallsStarted,
    ToolRecordReceived,
    ErrorFrame,
    ClientNoticeReceived,
    JobCompletionNotice,
    GoalChangedReceived,
    TaskChangedReceived,
    LocalNotice,
    OlderHistoryFailed,
    OlderHistoryLoaded,
    OlderHistoryRequested,
    TranscriptCleared,
    UsageSnapshotReceived,
    CompactionChanged,
    QueueReplaced,
    InteractionOpened,
    InteractionResolved,
    UserInputSubmitted,
    UserInputFailed,
    UserMessagePublished,
    RuntimeNoticePublished,
    JobUpdated,
    JobsReplaced,
]


__all__ = [
    "AssistantCompleted",
    "AssistantDelta",
    "ClientNoticeReceived",
    "CompactionChanged",
    "CompactionPayload",
    "ConnectionChanged",
    "ErrorFrame",
    "HistoryReplaced",
    "InteractionOpened",
    "InteractionRequest",
    "InteractionResolved",
    "InterruptAsked",
    "InterruptSettled",
    "JobCompletionNotice",
    "GoalChangedReceived",
    "TaskChangedReceived",
    "LocalNotice",
    "TranscriptCleared",
    "JobUpdated",
    "JobsReplaced",
    "QueueReplaced",
    "SessionConfigured",
    "SnapshotAdopted",
    "StatusSlotsUpdated",
    "StreamGapDetected",
    "StartedToolCall",
    "ToolCallsStarted",
    "ToolRecordReceived",
    "TurnCancelled",
    "TurnFinished",
    "TurnStarted",
    "UiEvent",
    "UsageSnapshotReceived",
    "UserInputFailed",
    "UserInputSubmitted",
    "UserMessagePublished",
    "RuntimeNoticePublished",
    "ThreadRead",
]
