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
from XBotv2.permissions.protocol import PermissionRequestData
from XBotv2.protocol import ServerEvent
from XBotv2.session.contracts import PendingInteractionData
from XBotv2.session.protocol import (
    AgentConfiguredData,
    HistoryUpdatedData,
    MessageData,
    QueueUpdatedData,
)
from XBotv2.tui.events import (
    AssistantCompleted,
    AssistantDelta,
    ClientNotice,
    CompactionChanged,
    ErrorFrame,
    HistoryReplaced,
    InteractionOpened,
    InteractionResolved,
    JobCompletionNotice,
    JobUpdated,
    QueueReplaced,
    SessionConfigured,
    StatusSlotsUpdated,
    ToolCallsStarted,
    ToolResult,
    TurnCancelled,
    TurnFinished,
    TurnStarted,
    UiEvent,
    UsageUpdated,
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
    "agent/inbox/spliced": (
        "canonical agent event; input_accepted/queue_updated are the client projections"
    ),
    "input_accepted": "queue_updated carries the resulting queue",
    "input_claimed": "queue_updated and message carry what the UI shows",
    "input_consumed": "queue_updated and message carry what the UI shows",
    "tool_call_delta": (
        "a tool is shown once tool_calls_started delivers its final arguments"
    ),
    # Contract finding: `permission_denied` is in the repository's SSE contract
    # fixture but has no producer in the current server, and its payload
    # (`decision: "deny"` plus `reason`) does not satisfy any existing payload
    # model -- `PermissionRequestData` pins `decision` to `Literal["ask"]`, and
    # `InteractionRecordedData` requires a `status` the frame does not carry.
    # Denials the server actually publishes arrive as
    # `permission_response_recorded`. Recorded here rather than guessed at.
    "permission_denied": (
        "legacy frame with no producer; recorded answers arrive as "
        "permission_response_recorded"
    ),
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


# --- one-to-one frames ----------------------------------------------------


_FRAMES: dict[str, tuple[type[BaseModel], Builder]] = {
    "assistant_message_delta": (AssistantMessageDeltaData, _carries(AssistantDelta)),
    "tool_result": (ToolResultData, _carries(ToolResult)),
    "error": (ErrorEventData, _carries(ErrorFrame)),
    "message": (MessageData, _carries(UserMessagePublished)),
    "agent_configured": (AgentConfiguredData, _carries(SessionConfigured)),
    "usage": (UsageData, _carries(UsageUpdated)),
    "queue_updated": (QueueUpdatedData, _carries(QueueReplaced)),
    "history_updated": (HistoryUpdatedData, _carries(HistoryReplaced)),
    "tool_calls_started": (ToolCallsStartedData, _carries(ToolCallsStarted)),
    "compaction_started": (CompactionStartedData, _carries(CompactionChanged)),
    "compaction_completed": (CompactionCompletedData, _carries(CompactionChanged)),
    "compaction_failed": (CompactionFailedData, _carries(CompactionChanged)),
    "job_updated": (JobSnapshot, _carries(JobUpdated)),
    "client_message": (ClientMessageData, _carries(ClientNotice)),
    "completion_notice": (JobCompletionData, _carries(JobCompletionNotice)),
    "permission_request": (PermissionRequestData, _opens()),
    "user_input_required": (UserInputRequiredData, _opens()),
}


# --- frames that need more than a payload swap ----------------------------


def _turn_started(payload: TurnData) -> tuple[UiEvent, ...]:
    return _turn((TurnStarted(payload=payload),), payload)


def _turn_finished(payload: TurnData) -> tuple[UiEvent, ...]:
    return _turn((TurnFinished(payload=payload),), payload)


def _turn_cancelled(payload: TurnCancelledData) -> tuple[UiEvent, ...]:
    return _turn((TurnCancelled(payload=payload),), payload)


def _turn(events: tuple[UiEvent, ...], payload: TurnData) -> tuple[UiEvent, ...]:
    """Status slots travel beside the turn frame that carries them."""
    if payload.status_slots:
        return (events[0], StatusSlotsUpdated(payload.status_slots))
    return events


def _assistant_message(payload: AssistantMessageData) -> tuple[UiEvent, ...]:
    """A tool-only answer carries no content, so its calls are materialized here."""
    events: list[UiEvent] = [AssistantCompleted(payload=payload)]
    if payload.tool_calls:
        events.append(ToolCallsStarted(payload=ToolCallsStartedData(
            tool_calls=[
                ToolCallStartedItem.model_validate(call) for call in payload.tool_calls
            ],
        )))
    return tuple(events)


_SPECIAL: Mapping[str, tuple[type[BaseModel], Builder]] = {
    "turn_started": (TurnData, _turn_started),
    "turn_finished": (TurnData, _turn_finished),
    "turn_cancelled": (TurnCancelledData, _turn_cancelled),
    "assistant_message": (AssistantMessageData, _assistant_message),
    "permission_response_recorded": (InteractionRecordedData, _carries(InteractionResolved)),
    "user_input_recorded": (InteractionRecordedData, _carries(InteractionResolved)),
}

FRAMES: Mapping[str, tuple[type[BaseModel], Builder]] = {**_FRAMES, **_SPECIAL}

_INTERACTION_FRAMES: Mapping[str, tuple[type[BaseModel], Builder]] = {
    "permission_request": (PermissionRequestData, _opens()),
    "user_input_required": (UserInputRequiredData, _opens()),
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
        entry = FRAMES.get(frame.type)
        if entry is None:
            if frame.type in IGNORED_FRAMES:
                return ()
            raise UnsupportedFrame(f"unsupported frame type: {frame.type!r}")
        return _apply(entry, frame.data)


def replay_pending_interactions(
    items: Iterable[PendingInteractionData],
) -> tuple[UiEvent, ...]:
    """Rebuild unanswered dialogs from a session snapshot.

    A live prompt exists only in the event stream, so a reconnecting client
    replays the snapshot's pending interactions through the same translation as a
    live frame; the reducer then cannot tell the two apart.
    """
    events: list[UiEvent] = []
    for item in items:
        entry = _INTERACTION_FRAMES.get(item.type)
        if entry is None:
            raise UnsupportedFrame(f"unsupported pending interaction: {item.type!r}")
        events.extend(_apply(entry, item.data))
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
