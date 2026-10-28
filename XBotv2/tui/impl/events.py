"""Typed inputs to the session reducer.

Everything that can change the UI arrives here first: server frames (translated
by ``protocol.py``), local submissions, interrupt results, watchdog readings.

Two rules keep this module from re-declaring the protocol:

* validated producer models enter the reducer directly where they uniquely
  describe the transition. The wire shape is defined once, by its owner.
* only genuinely client-side events declare their own fields: a submission, an
  interrupt request, a gap, or a connection change.

Nothing here imports Textual or performs IO, so the whole reducer is testable in
isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union
from XBotv2.core.api.history import HistoryPage
from XBotv2.session.api.records import (
    DisplayRecord,
    AssistantRecord,
    HumanInputRecord,
    RuntimeNoticeRecord,
    ToolRecord,
)
from XBotv2.agentloop.api.outputs import (
    AssistantReasoningDelta,
    AssistantTextDelta,
    LoopError,
    LoopTurnEnded,
    LoopTurnStarted,
    ToolCallsStarted as LoopToolCallsStarted,
)
from XBotv2.compact.api.protocol import CompactionCompleted, CompactionFailed, CompactionStarted
from XBotv2.usage.api.events import UsageUpdated
from XBotv2.interactions.api.models import ClientNotice, UserInputRequest, UserInputRecorded
from XBotv2.jobs.api.protocol import JobCompletedEvent, JobListResponse, JobUpdatedEvent
from XBotv2.goal.api.events import GoalChanged
from XBotv2.todolist.api.events import TaskChanged
from XBotv2.session.api.contracts import ThreadSummary
from XBotv2.permissions.api.models import PermissionRequest
from XBotv2.permissions.api.protocol import PermissionResponseRecorded
from XBotv2.session.api.events import (
    AgentConfiguredEvent,
    HistoryUpdatedEvent,
    InputConsumedEvent,
    QueueReplacedEvent,
)
from XBotv2.session.api.protocol import OpenSessionResponse
from XBotv2.tui.impl.status import Connection, ServerTurn

InteractionRequest = Union[PermissionRequest, UserInputRequest]


# --- transport and session lifecycle --------------------------------------


@dataclass(frozen=True)
class ConnectionChanged:
    connection: Connection
    reason: str = ""


@dataclass(frozen=True)
class DisplayHistoryLoaded:
    payload: HistoryPage[DisplayRecord]


@dataclass(frozen=True)
class StreamGapDetected:
    """The frame sequence skipped; the user must be told."""

    expected: int = 0
    received: int = 0


# --- turn lifecycle -------------------------------------------------------


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
    media_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class UserInputAccepted:
    input_id: str


@dataclass(frozen=True)
class UserInputFailed:
    input_id: str
    error: str


UiEvent = Union[
    DisplayHistoryLoaded,
    ConnectionChanged,
    OpenSessionResponse,
    AgentConfiguredEvent,
    ThreadSummary,
    StreamGapDetected,
    LoopTurnStarted,
    TurnFinished,
    TurnCancelled,
    InterruptAsked,
    InterruptSettled,
    AssistantTextDelta | AssistantReasoningDelta,
    AssistantRecord,
    HistoryUpdatedEvent,
    LoopToolCallsStarted,
    ToolRecord,
    LoopError,
    ClientNotice,
    JobCompletedEvent,
    GoalChanged,
    TaskChanged,
    LocalNotice,
    TranscriptCleared,
    UsageUpdated,
    CompactionStarted,
    CompactionCompleted,
    CompactionFailed,
    QueueReplacedEvent,
    InteractionOpened,
    InteractionResolved,
    UserInputSubmitted,
    UserInputAccepted,
    InputConsumedEvent,
    UserInputFailed,
    HumanInputRecord,
    RuntimeNoticeRecord,
    JobUpdatedEvent,
    JobListResponse,
]
