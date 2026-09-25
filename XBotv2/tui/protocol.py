"""Wire frames become reducer events; bad envelopes are rejected, never skipped.

Two jobs:

* **validate the envelope** -- a frame that belongs to another session or thread,
  or whose type this client does not know, is an error. The old client dispatched
  on a string and ignored anything it did not recognise, which let a whole class
  of server behaviour stay invisible.
* **translate the payload** -- each frame maps to a reducer event through the
  producer's own pydantic payload model, so a version skew fails loudly at the
  boundary instead of half-applying.

The frame table is declarative: a type names its payload model and the event that
carries it. The model does the validation, so no payload field is restated here.
Frames the client deliberately does not act on are listed with the reason and a
test each; every other unknown type is rejected.

The server has no public catalogue of its event types, so this table is the
client's declared contract. ``test_protocol.py`` cross-checks it against the
repository's own SSE contract fixture, so a new server frame cannot slip past
unnoticed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from pydantic import BaseModel, ValidationError

from XBotv2.agentloop.protocol import (
    AssistantReasoningDelta,
    AssistantTextDelta,
    LoopError,
    LoopTurnEnded,
    LoopTurnStarted,
    ToolCallsStarted as LoopToolCallsStarted,
    TurnCancelled as CancelledOutcome,
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
from XBotv2.jobs.protocol import JobCompletedEvent, JobUpdatedEvent
from XBotv2.goal.models import GoalChanged
from XBotv2.todolist.contracts import TaskChanged
from XBotv2.permissions.contracts import PermissionRequest
from XBotv2.permissions.protocol import PermissionResponseRecorded
from XBotv2.protocol import ServerEvent
from XBotv2.interactions.contracts import InteractionRequest
from XBotv2.session.protocol import (
    AgentConfiguredData,
    HistoryUpdatedEvent,
    QueueUpdatedData,
)
from XBotv2.session.records import (
    AssistantRecord,
    HumanInputRecord,
    InputRecordPayload,
    RuntimeNoticeRecord,
    ToolRecord,
)
from XBotv2.tui.events import (
    AssistantCompleted,
    AssistantDelta,
    ClientNoticeReceived,
    CompactionChanged,
    ErrorFrame,
    HistoryReplaced,
    InteractionOpened,
    InteractionResolved,
    JobCompletionNotice,
    GoalChangedReceived,
    TaskChangedReceived,
    JobUpdated,
    QueueReplaced,
    RuntimeNoticePublished,
    SessionConfigured,
    StatusSlotsUpdated,
    ToolCallsStarted,
    ToolRecordReceived,
    TurnCancelled,
    TurnFinished,
    TurnStarted,
    UiEvent,
    UsageSnapshotReceived,
    UserMessagePublished,
)


class FrameRejected(Exception):
    """A frame that must not be applied to the session."""


class ForeignFrame(FrameRejected):
    """The frame belongs to a different session or thread."""


class UnsupportedFrame(FrameRejected):
    """The frame type is not part of this client's contract."""


# Frames the client receives but does not act on, each with the frame that
# supersedes it. Anything in neither this table nor ``FRAMES`` is an error.
IGNORED_FRAMES: Mapping[str, str] = {
    "end": "stream terminator; the transport consumes it",
    "agent/inbox/changed": (
        "canonical agent event; input_accepted/queue_updated are the client projections"
    ),
    "input_accepted": "queue_updated carries the resulting queue",
    "input_claimed": "queue_updated and message carry what the UI shows",
    "input_consumed": "queue_updated and message carry what the UI shows",
    "tool_call_delta": (
        "a tool is shown once tool_calls_started delivers its final arguments"
    ),
    "usage": "usage_updated carries the authoritative cumulative snapshot",
}

Payload = Mapping[str, Any]
Builder = Callable[[BaseModel], tuple[UiEvent, ...]]


def _carries(event_cls: type) -> Builder:
    """The common case: the payload model *is* the event's payload."""

    def build(payload: BaseModel) -> tuple[UiEvent, ...]:
        return (event_cls(payload=payload),)

    return build


def _opens() -> Builder:
    def build(payload: BaseModel) -> tuple[UiEvent, ...]:
        return (InteractionOpened(request=payload),)

    return build


def _published_input(payload: InputRecordPayload) -> tuple[UiEvent, ...]:
    record = payload.root
    if isinstance(record, HumanInputRecord):
        return (UserMessagePublished(payload=record),)
    if isinstance(record, RuntimeNoticeRecord):
        return (RuntimeNoticePublished(payload=record),)
    raise TypeError(f"Unsupported input record: {type(record).__name__}")


# --- one-to-one frames ----------------------------------------------------


_FRAMES: dict[str, tuple[type[BaseModel], Builder]] = {
    "assistant_text_delta": (AssistantTextDelta, _carries(AssistantDelta)),
    "assistant_reasoning_delta": (
        AssistantReasoningDelta,
        _carries(AssistantDelta),
    ),
    "tool_completed": (ToolRecord, _carries(ToolRecordReceived)),
    "error": (LoopError, _carries(ErrorFrame)),
    "message": (InputRecordPayload, _published_input),
    "agent_configured": (AgentConfiguredData, _carries(SessionConfigured)),
    "usage_updated": (UsageUpdated, _carries(UsageSnapshotReceived)),
    "queue_updated": (QueueUpdatedData, _carries(QueueReplaced)),
    "history_updated": (HistoryUpdatedEvent, _carries(HistoryReplaced)),
    "tool_calls_started": (LoopToolCallsStarted, _carries(ToolCallsStarted)),
    "compaction_started": (CompactionStarted, _carries(CompactionChanged)),
    "compaction_completed": (CompactionCompleted, _carries(CompactionChanged)),
    "compaction_failed": (CompactionFailed, _carries(CompactionChanged)),
    "job_updated": (JobUpdatedEvent, lambda payload: (JobUpdated(payload=payload.view),)),
    "client_message": (ClientNotice, _carries(ClientNoticeReceived)),
    "job_completed": (JobCompletedEvent, _carries(JobCompletionNotice)),
    "goal_changed": (GoalChanged, _carries(GoalChangedReceived)),
    "task_changed": (TaskChanged, _carries(TaskChangedReceived)),
    "permission_request": (PermissionRequest, _opens()),
    "user_input_required": (UserInputRequest, _opens()),
}


# --- frames that need more than a payload swap ----------------------------


def _turn_started(payload: LoopTurnStarted) -> tuple[UiEvent, ...]:
    return (TurnStarted(payload=payload),)


def _turn_ended(payload: LoopTurnEnded) -> tuple[UiEvent, ...]:
    if isinstance(payload.outcome, CancelledOutcome):
        return (TurnCancelled(payload=payload),)
    return (TurnFinished(payload=payload),)


def _assistant_message(payload: AssistantRecord) -> tuple[UiEvent, ...]:
    return (AssistantCompleted(payload=payload),)


_SPECIAL: Mapping[str, tuple[type[BaseModel], Builder]] = {
    "turn_started": (LoopTurnStarted, _turn_started),
    "turn_ended": (LoopTurnEnded, _turn_ended),
    "assistant_completed": (AssistantRecord, _assistant_message),
    "permission_response_recorded": (
        PermissionResponseRecorded,
        _carries(InteractionResolved),
    ),
    "user_input_recorded": (UserInputRecorded, _carries(InteractionResolved)),
}

FRAMES: Mapping[str, tuple[type[BaseModel], Builder]] = {**_FRAMES, **_SPECIAL}

_INTERACTION_FRAMES: Mapping[str, tuple[type[BaseModel], Builder]] = {
    "permission_request": (PermissionRequest, _opens()),
    "user_input_required": (UserInputRequest, _opens()),
}


@dataclass(frozen=True)
class FrameTranslator:
    """Translates frames for one attached session and thread."""

    session_id: str = ""
    thread_id: str = ""

    def translate(self, frame: ServerEvent) -> tuple[UiEvent, ...]:
        if frame.session_id and self.session_id and frame.session_id != self.session_id:
            raise ForeignFrame(
                f"frame for session {frame.session_id!r}, attached to {self.session_id!r}"
            )
        if frame.thread_id and self.thread_id and frame.thread_id != self.thread_id:
            raise ForeignFrame(
                f"frame for thread {frame.thread_id!r}, attached to {self.thread_id!r}"
            )
        entry = FRAMES.get(frame.kind)
        if entry is None:
            if frame.kind in IGNORED_FRAMES:
                return ()
            raise UnsupportedFrame(f"unsupported frame kind: {frame.kind!r}")
        return _apply(entry, frame.payload)


def replay_pending_interactions(
    items: Iterable[InteractionRequest],
) -> tuple[UiEvent, ...]:
    """Rebuild unanswered dialogs from a session snapshot.

    A live prompt exists only in the event stream, so a reconnecting client
    replays the snapshot's pending interactions through the same translation as a
    live frame; the reducer then cannot tell the two apart.
    """
    events: list[UiEvent] = []
    for item in items:
        entry = _INTERACTION_FRAMES.get(item.kind)
        if entry is None:
            raise UnsupportedFrame(f"unsupported pending interaction: {item.kind!r}")
        events.append(InteractionOpened(request=item))
    return tuple(events)


def _apply(
    entry: tuple[type[BaseModel], Builder],
    data: Payload,
) -> tuple[UiEvent, ...]:
    model, build = entry
    try:
        return tuple(build(model.model_validate(data)))
    except ValidationError as exc:
        raise FrameRejected(f"malformed payload: {exc}") from exc


__all__ = [
    "FRAMES",
    "IGNORED_FRAMES",
    "ForeignFrame",
    "FrameRejected",
    "FrameTranslator",
    "UnsupportedFrame",
    "replay_pending_interactions",
]
