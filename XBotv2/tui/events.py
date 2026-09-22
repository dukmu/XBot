"""Typed inputs to the session reducer.

Everything that can change the UI arrives here first: server frames (translated
by ``protocol.py``), local submissions, interrupt results, watchdog readings.

Two rules keep this module from re-declaring the protocol:

* a frame-shaped event **carries the producer's payload model** (``TurnData``,
  ``AssistantMessageData``, ``ToolResultData``, ``CompactionCompletedData``, ...)
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
    AssistantMessageData,
    AssistantMessageDeltaData,
    ErrorEventData,
    ToolCallStartedItem,
    ToolCallsStartedData,
    ToolResultData,
    TurnCancelledData,
    TurnData,
)
from XBotv2.compact.protocol import (
    CompactionCompletedData,
    CompactionFailedData,
    CompactionStartedData,
)
from XBotv2.core.usage import UsageData
from XBotv2.interactions.protocol import (
    ClientMessageData,
    InteractionRecordedData,
    UserInputRequiredData,
)
from XBotv2.jobs.contracts import JobSnapshot
from XBotv2.jobs.protocol import JobCompletionData
from XBotv2.session.contracts import ThreadSummary
from XBotv2.permissions.protocol import PermissionRequestData
from XBotv2.session.protocol import (
    AgentConfiguredData,
    HistoryUpdatedData,
    MessageData,
    OpenSessionResponse,
    QueueUpdatedData,
)
from XBotv2.tui.status import Connection, ServerTurn

InteractionRequest = Union[PermissionRequestData, UserInputRequiredData]
CompactionPayload = Union[
    CompactionStartedData, CompactionCompletedData, CompactionFailedData
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
    payload: TurnData


@dataclass(frozen=True)
class TurnFinished:
    payload: TurnData


@dataclass(frozen=True)
class TurnCancelled:
    payload: TurnCancelledData


@dataclass(frozen=True)
class InterruptAsked:
    pass


@dataclass(frozen=True)
class InterruptSettled:
    cancelled: bool = False


# --- conversation content -------------------------------------------------


@dataclass(frozen=True)
class AssistantDelta:
    payload: AssistantMessageDeltaData


@dataclass(frozen=True)
class AssistantCompleted:
    payload: AssistantMessageData


@dataclass(frozen=True)
class HistoryReplaced:
    payload: HistoryUpdatedData


@dataclass(frozen=True)
class ToolCallsStarted:
    payload: ToolCallsStartedData


@dataclass(frozen=True)
class ToolResult:
    payload: ToolResultData


@dataclass(frozen=True)
class ErrorFrame:
    payload: ErrorEventData


@dataclass(frozen=True)
class ClientNotice:
    """A server-authored line for the transcript (``client_message``)."""

    payload: ClientMessageData


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

    payload: JobCompletionData


# --- usage, compaction, queue --------------------------------------------


@dataclass(frozen=True)
class UsageUpdated:
    payload: UsageData


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
    payload: InteractionRecordedData


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

    payload: MessageData


# --- background work ------------------------------------------------------


@dataclass(frozen=True)
class JobUpdated:
    """One job snapshot. The server publishes jobs one at a time."""

    payload: JobSnapshot


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
    ToolResult,
    ErrorFrame,
    ClientNotice,
    JobCompletionNotice,
    LocalNotice,
    TranscriptCleared,
    UsageUpdated,
    CompactionChanged,
    QueueReplaced,
    InteractionOpened,
    InteractionResolved,
    UserInputSubmitted,
    UserInputFailed,
    UserMessagePublished,
    JobUpdated,
]


__all__ = [
    "AssistantCompleted",
    "AssistantDelta",
    "ClientNotice",
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
    "LocalNotice",
    "TranscriptCleared",
    "JobUpdated",
    "QueueReplaced",
    "SessionConfigured",
    "SnapshotAdopted",
    "StatusSlotsUpdated",
    "StreamGapDetected",
    "ToolCallStartedItem",
    "ToolCallsStarted",
    "ToolResult",
    "TurnCancelled",
    "TurnFinished",
    "TurnStarted",
    "UiEvent",
    "UsageUpdated",
    "UserInputFailed",
    "UserInputSubmitted",
    "UserMessagePublished",
    "ThreadRead",
]
