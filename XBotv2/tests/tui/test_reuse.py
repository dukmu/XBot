"""Guards that keep the reuse real instead of aspirational.

The rewrite's rule is: a wire shape is declared once, by its owner, and the TUI
carries it. These tests make that rule checkable, so a later change cannot quietly
reintroduce a hand-copied field list or a locally declared payload model.

They also tie the client's own vocabularies (turn status, tool status) to the
server's ``Literal`` types, so the two cannot drift apart.
"""

from __future__ import annotations

from typing import get_args, get_type_hints

import pytest
from pydantic import BaseModel

from XBotv2.agentloop.protocol import ToolResultData
from XBotv2.session.contracts import ThreadSummary
from XBotv2.tui import events, protocol
from XBotv2.tui.events import (
    AssistantCompleted,
    AssistantDelta,
    ClientNotice,
    CompactionChanged,
    ConnectionChanged,
    ErrorFrame,
    HistoryReplaced,
    InteractionOpened,
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
    ThreadRead,
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
)
from XBotv2.tui.state import MAIN_THREAD_KIND, SUBAGENT_THREAD_KIND
from XBotv2.tui.status import ServerTurn
from XBotv2.tui.timeline import ToolStatus

# Events that carry a protocol payload, and events the client originates itself.
# Every member of the union must be in exactly one of these sets.
SERVER_SHAPED = {
    SnapshotAdopted,
    SessionConfigured,
    TurnStarted,
    TurnFinished,
    TurnCancelled,
    AssistantDelta,
    AssistantCompleted,
    HistoryReplaced,
    ToolCallsStarted,
    ToolResult,
    ErrorFrame,
    ClientNotice,
    JobCompletionNotice,
    UsageUpdated,
    CompactionChanged,
    QueueReplaced,
    InteractionOpened,
    InteractionResolved,
    UserMessagePublished,
    JobUpdated,
    ThreadRead,
}

CLIENT_SIDE = {
    ConnectionChanged,
    LocalNotice,
    TranscriptCleared,
    StatusSlotsUpdated,
    StreamGapDetected,
    InterruptAsked,
    InterruptSettled,
    UserInputSubmitted,
    UserInputFailed,
}


def carried_models(cls: type) -> set[type]:
    """Protocol models this event holds, however it names the field."""
    found: set[type] = set()
    for hint in get_type_hints(cls).values():
        for member in get_args(hint) or (hint,):
            if isinstance(member, type) and issubclass(member, BaseModel):
                found.add(member)
    return found


def test_every_event_is_classified_as_server_shaped_or_client_side() -> None:
    """A new event must be classified deliberately: either it carries a protocol
    payload or it is a client-side concept."""
    assert set(get_args(UiEvent)) == SERVER_SHAPED | CLIENT_SIDE
    assert not (SERVER_SHAPED & CLIENT_SIDE)


@pytest.mark.parametrize("cls", sorted(SERVER_SHAPED, key=lambda c: c.__name__))
def test_server_shaped_events_carry_a_protocol_model(cls: type) -> None:
    """No frame-shaped event may restate its payload's fields."""
    assert carried_models(cls), f"{cls.__name__} carries no protocol model"


@pytest.mark.parametrize("cls", sorted(CLIENT_SIDE, key=lambda c: c.__name__))
def test_client_side_events_do_not_smuggle_a_wire_shape(cls: type) -> None:
    assert not carried_models(cls), (
        f"{cls.__name__} holds a protocol model but is declared client-side"
    )


@pytest.mark.parametrize("frame_type", sorted(protocol.FRAMES))
def test_frame_table_uses_repository_payload_models(frame_type: str) -> None:
    """The declared payload model must come from the package that owns the wire
    shape, never from the TUI itself."""
    model, _builder = protocol.FRAMES[frame_type]
    module = model.__module__
    assert module.startswith("XBotv2."), f"{frame_type} uses {module}"
    assert not module.startswith("XBotv2.tui"), (
        f"{frame_type} uses a locally declared payload model ({module})"
    )


def _literal_values(annotation: object) -> set[str]:
    return {str(member) for member in get_args(annotation)}


def test_turn_status_vocabulary_covers_the_wire() -> None:
    wire = _literal_values(ThreadSummary.model_fields["turn_status"].annotation)
    assert wire <= {status.value for status in ServerTurn}


def test_tool_status_vocabulary_covers_the_wire() -> None:
    """The client's tool states are the server's result statuses plus the two
    states a call passes through before its result arrives."""
    wire = _literal_values(ToolResultData.model_fields["status"].annotation)
    assert wire <= set(get_args(ToolStatus))
    assert {"pending", "running"} <= set(get_args(ToolStatus))


def test_events_module_declares_no_payload_models_of_its_own() -> None:
    """Every BaseModel-typed annotation in ``events`` must be imported from the
    package that owns it."""
    owners = set()
    for cls in get_args(UiEvent):
        for model in carried_models(cls):
            owners.add(model.__module__)
    assert owners, "the union should carry protocol models"
    assert all(not module.startswith("XBotv2.tui") for module in owners), owners
    assert not hasattr(events, "SessionSnapshot"), (
        "a locally declared snapshot shape used to live here"
    )


def test_the_read_only_rule_speaks_the_wire_thread_kind() -> None:
    """A subagent thread is read-only because the server says it is a subagent.

    The client owns no vocabulary here: its two words are exactly the wire's.
    """
    wire = _literal_values(ThreadSummary.model_fields["kind"].annotation)
    assert wire == {MAIN_THREAD_KIND, SUBAGENT_THREAD_KIND}
