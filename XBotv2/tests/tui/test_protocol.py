"""Wire frames are translated, and envelopes are checked.

The translator is the boundary where the server's contracts become the reducer's
input. Three rules matter:

* a frame for another session or thread, or a frame type this client does not
  know, is an error -- never a silent skip;
* the payload is validated by its own producer's model, so no field is restated
  here and a version skew fails at the boundary;
* every frame type the repository's own SSE contract fixture publishes is either
  translated or listed as deliberately ignored, so a new server frame cannot
  reach the client unnoticed.
"""

from __future__ import annotations

import pytest

from XBotv2.compact.protocol import CompactionStarted
from XBotv2.protocol import ServerEvent, server_event
from XBotv2.permissions.contracts import PermissionRequest, ToolPermission
from XBotv2.core.tools import ToolCall
from XBotv2.core.domain import SessionScope
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
    JobUpdated,
    QueueReplaced,
    SessionConfigured,
    ToolCallsStarted,
    ToolRecordReceived,
    TurnCancelled,
    TurnFinished,
    TurnStarted,
    UserMessagePublished,
)
from XBotv2.tui.protocol import (
    IGNORED_FRAMES,
    ForeignFrame,
    FrameRejected,
    FrameTranslator,
    UnsupportedFrame,
    replay_pending_interactions,
)

SESSION = "s1"
THREAD = "agent"


def translator() -> FrameTranslator:
    return FrameTranslator(session_id=SESSION, thread_id=THREAD)


def frame(frame_type: str, data: dict, **overrides) -> ServerEvent:
    base = {"session_id": SESSION, "thread_id": THREAD, "sequence": 1}
    return server_event(
        kind=frame_type, payload=data, scope=SessionScope(), **{**base, **overrides}
    )


def only(frame_type: str, data: dict) -> object:
    events = translator().translate(frame(frame_type, data))
    assert len(events) == 1, f"{frame_type} produced {len(events)} events"
    return events[0]


def payload_of(frame_type: str, data: dict):
    event = only(frame_type, data)
    return getattr(event, "payload")


# --- envelope validation --------------------------------------------------


def test_frame_for_another_session_is_rejected() -> None:
    with pytest.raises(ForeignFrame):
        translator().translate(frame("turn_started", {"turn": 1}, session_id="other"))


def test_frame_for_another_thread_is_rejected() -> None:
    with pytest.raises(ForeignFrame):
        translator().translate(frame("turn_started", {"turn": 1}, thread_id="agent-sub"))


def test_frame_without_session_or_thread_identity_is_accepted() -> None:
    events = translator().translate(
        server_event(
            kind="turn_started", payload={"turn": 2}, sequence=1,
            session_id="", thread_id="", scope=SessionScope(),
        )
    )
    assert len(events) == 1
    assert isinstance(events[0], TurnStarted)
    assert events[0].payload.turn == 2


def test_unknown_frame_type_is_rejected_not_ignored() -> None:
    with pytest.raises(UnsupportedFrame):
        translator().translate(frame("something_new", {}))


def test_malformed_payload_is_rejected() -> None:
    with pytest.raises(FrameRejected):
        translator().translate(frame("turn_started", {"turn": "not a number"}))


def test_stream_end_is_not_a_state_event() -> None:
    assert translator().translate(frame("end", {"status": "closed"})) == ()


# --- turn lifecycle -------------------------------------------------------


def test_turn_started_carries_only_the_loop_turn() -> None:
    events = translator().translate(frame("turn_started", {"turn": 3}))
    assert isinstance(events[0], TurnStarted)
    assert events[0].payload.turn == 3


def test_turn_started_rejects_fields_owned_by_other_contracts() -> None:
    with pytest.raises(FrameRejected):
        translator().translate(
            frame("turn_started", {"turn": 3, "status_slots": {"goal": "ship"}})
        )


def test_turn_started_without_slots_is_only_a_turn_event() -> None:
    assert payload_of("turn_started", {"turn": 3}).turn == 3
    assert len(translator().translate(frame("turn_started", {"turn": 3}))) == 1


def test_turn_ended_carries_the_turn_and_outcome() -> None:
    event = only(
        "turn_ended",
        {"turn": 3, "outcome": {"kind": "finished", "stop_reason": "completed"}},
    )
    assert isinstance(event, TurnFinished)
    assert event.payload.turn == 3


def test_cancelled_turn_uses_the_typed_terminal_outcome() -> None:
    event = only(
        "turn_ended",
        {
            "turn": 3,
            "outcome": {"kind": "cancelled", "reason": "client_interrupt"},
        },
    )
    assert isinstance(event, TurnCancelled)
    assert event.payload.outcome.reason == "client_interrupt"


# --- assistant content ----------------------------------------------------


def test_assistant_text_delta_translates_typed_text() -> None:
    event = only("assistant_text_delta", {"text": "hi"})
    assert isinstance(event, AssistantDelta)
    assert event.payload.text == "hi"


def test_assistant_reasoning_delta_remains_typed_reasoning() -> None:
    event = only("assistant_reasoning_delta", {"text": "why"})
    assert isinstance(event, AssistantDelta)
    assert event.payload.text == "why"


def test_assistant_completed_translates_the_canonical_record() -> None:
    event = only(
        "assistant_completed",
        {
            "id": "a1",
            "content": "done",
            "reasoning": "",
            "tool_calls": [],
            "timing": {"total_ms": 1},
            "stop": {"kind": "completed"},
        },
    )
    assert isinstance(event, AssistantCompleted)
    assert event.payload.id == "a1"
    assert event.payload.content == "done"


def test_assistant_completed_keeps_its_calls_in_the_assistant_record() -> None:
    event = only(
        "assistant_completed",
        {
            "id": "a1",
            "content": "",
            "reasoning": "",
            "tool_calls": [{"id": "c1", "name": "bash", "args": {"command": "ls"}}],
            "timing": {"total_ms": 1},
            "stop": {"kind": "tool_calls_requested"},
        },
    )
    assert isinstance(event, AssistantCompleted)
    assert event.payload.tool_calls[0].id == "c1"


def test_tool_calls_started_translates_every_call() -> None:
    event = only(
        "tool_calls_started",
        {
            "calls": [
                {"call": {"id": "c1", "name": "bash", "args": {}}, "category": "execute"},
                {"call": {"id": "c2", "name": "read", "args": {}}, "category": "read"},
            ]
        },
    )
    assert isinstance(event, ToolCallsStarted)
    assert [item.call.id for item in event.payload.calls] == ["c1", "c2"]


def test_tool_completed_translates_the_canonical_tool_record() -> None:
    event = only(
        "tool_completed",
        {
            "id": "tool-1",
            "call": {"id": "c1", "name": "bash"},
            "outcome": {
                "kind": "succeeded",
                "output": {"parts": [{"kind": "text", "text": "ok"}]},
            },
            "timing": {"duration_ms": 2},
        },
    )
    assert isinstance(event, ToolRecordReceived)
    assert event.payload.outcome.kind == "succeeded"


def test_tool_failure_remains_the_tool_outcome_variant() -> None:
    event = only(
        "tool_completed",
        {
            "id": "tool-1",
            "call": {"id": "c1", "name": "bash"},
            "outcome": {
                "kind": "failed",
                "error": {"code": "boom", "message": "no"},
                "output": {},
            },
            "timing": {"duration_ms": 2},
        },
    )
    assert isinstance(event, ToolRecordReceived)
    assert event.payload.outcome.kind == "failed"


# --- session projections --------------------------------------------------


def test_user_message_translates_to_a_published_message() -> None:
    event = only(
        "message",
        {"kind": "human_input", "id": "m1", "content": "hello"},
    )
    assert isinstance(event, UserMessagePublished)
    assert (event.payload.id, event.payload.content) == ("m1", "hello")


def test_queue_updated_replaces_the_queue() -> None:
    event = only(
        "queue_updated",
        {"items": [{"message_id": "q1", "content": "later", "target": "next-turn"}]},
    )
    assert isinstance(event, QueueReplaced)
    assert [item.message_id for item in event.payload.items] == ["q1"]


def test_agent_configured_translates_identity() -> None:
    event = only(
        "agent_configured",
        {"runtime_selection": {
            "agent_name": "Reviewer",
            "prompt": "",
            "limits": {},
            "enabled_tools": [],
            "model": {
                "route": {"provider": "p", "model": "m"},
                "generation": {
                    "mode": {"kind": "standard"},
                    "max_output_tokens": 1024,
                },
                "context_window": 4096,
            },
        }},
    )
    assert isinstance(event, SessionConfigured)
    assert event.payload.runtime_selection.agent_name == "Reviewer"


def test_history_updated_replaces_the_timeline() -> None:
    event = only(
        "history_updated",
        {
            "operation": "clear",
            "mutation": {
                "removed_turns": 1,
                "history": {
                    "items": [{"kind": "human_input", "id": "n1", "content": "kept"}],
                    "older_cursor": None,
                },
                "stats": {"turns": 1},
            },
        },
    )
    assert isinstance(event, HistoryReplaced)
    assert [item.content for item in event.payload.mutation.history.items] == ["kept"]


# --- usage and compaction -------------------------------------------------


def test_usage_updated_carries_the_canonical_snapshot() -> None:
    event = only("usage_updated", {"snapshot": {"total_counters": {"input": 10, "output": 4}}})
    from XBotv2.tui.events import UsageSnapshotReceived

    assert isinstance(event, UsageSnapshotReceived)
    assert event.payload.snapshot.total_counters.input == 10


def test_compaction_started_is_active_without_a_notice() -> None:
    event = only(
        "compaction_started",
        {
            "reason": "automatic",
            "messages_before": 10,
            "history_chars_before": 100,
            "context_tokens_before": 1000,
        },
    )
    assert isinstance(event, CompactionChanged)
    assert isinstance(event.payload, CompactionStarted), "started means active"


def test_compaction_completed_carries_the_summary() -> None:
    event = only(
        "compaction_completed",
        {
            "reason": "manual",
            "automatic": False,
            "summary": {"kind": "compaction_summary", "id": "summary-1", "summary": "kept the gist"},
            "metrics": _metrics(),
        },
    )
    assert isinstance(event, CompactionChanged)
    assert event.payload.summary.summary == "kept the gist"


def test_compaction_failed_carries_the_message() -> None:
    event = only("compaction_failed", {"reason": "automatic", "message": "no budget"})
    assert isinstance(event, CompactionChanged)
    assert event.payload.message == "no budget"


# --- interactions ---------------------------------------------------------


def test_permission_request_opens_a_permission_prompt() -> None:
    event = only(
        "permission_request",
        {
            "kind": "permission_request",
            "interaction_id": "r1",
            "source": "permission_system",
            "reason": "writes outside the workspace",
            "subject": {
                "kind": "tool",
                "tool_call": {"id": "c1", "name": "bash", "args": {}},
            },
            "resume_supported": False,
        },
    )
    assert isinstance(event, InteractionOpened)
    assert event.request.interaction_id == "r1"


def test_user_input_required_opens_a_question() -> None:
    event = only(
        "user_input_required",
        {
            "kind": "user_input_required",
            "interaction_id": "q1",
            "source": "ask_user",
            "tool_call_id": "c1",
            "question": "Which?",
            "options": [
                {"label": "A", "description": "one"},
                {"label": "B", "description": "two"},
            ],
            "resume_supported": False,
        },
    )
    assert isinstance(event, InteractionOpened)
    assert len(event.request.options) == 2


def test_permission_response_recorded_resolves() -> None:
    event = only(
        "permission_response_recorded",
        {
            "kind": "permission_response_recorded",
            "interaction_id": "r1",
            "approval": {"kind": "allowed", "scope": "once"},
        },
    )
    assert isinstance(event, InteractionResolved)
    assert event.payload.interaction_id == "r1"
    assert event.payload.approval.kind == "allowed"


def test_user_input_recorded_resolves() -> None:
    event = only(
        "user_input_recorded",
        {
            "kind": "user_input_recorded",
            "interaction_id": "q1",
            "resolution": {"kind": "timeout", "reason": "expired"},
        },
    )
    assert isinstance(event, InteractionResolved)
    assert event.payload.interaction_id == "q1"


def test_client_message_becomes_a_notice_with_its_level() -> None:
    event = only("client_message", {"message": "heads up", "level": "warning", "source": "compact"})
    assert isinstance(event, ClientNoticeReceived)
    assert event.payload.message == "heads up"
    assert event.payload.level == "warning"


def test_job_updated_translates_a_snapshot() -> None:
    event = only("job_updated", {"view": {
        "id": "j1", "kind": "agent", "label": "review", "state": "running", "elapsed_ms": 1,
    }})
    assert isinstance(event, JobUpdated)
    assert event.payload.id == "j1"


def test_job_completion_notice_does_not_open_a_turn() -> None:
    event = only(
        "job_completed",
        {"view": {
            "id": "j1", "kind": "agent", "label": "review", "state": "succeeded", "elapsed_ms": 2,
        }},
    )
    assert isinstance(event, JobCompletionNotice)
    assert event.payload.view.id == "j1"


# --- deliberately ignored frames -----------------------------------------


@pytest.mark.parametrize("frame_type", sorted(IGNORED_FRAMES))
def test_ignored_frames_return_nothing(frame_type: str) -> None:
    """Each ignored frame is superseded by one the client does act on; the frame
    table carries the reason, and this test keeps the list honest."""
    assert translator().translate(frame(frame_type, {})) == ()


# --- snapshot interaction replay -----------------------------------------


def test_pending_interactions_from_a_snapshot_are_replayed() -> None:
    events = replay_pending_interactions(
        (
            PermissionRequest(
                interaction_id="r1",
                source="permission_system",
                reason="needs approval",
                subject=ToolPermission(
                    tool_call=ToolCall(id="c1", name="bash", args={}),
                ),
            ),
        )
    )
    assert len(events) == 1
    assert isinstance(events[0], InteractionOpened)


def test_an_unreplayable_pending_interaction_is_rejected() -> None:
    with pytest.raises(ValueError):
        PermissionRequest(
            interaction_id="",
            source="permission_system",
            reason="needs approval",
            subject=ToolPermission(tool_call=ToolCall(id="c1", name="bash", args={})),
        )


def test_an_unknown_pending_interaction_is_rejected() -> None:
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class UnknownRequest:
        interaction_id: str = "r1"
        kind: str = "unknown"
        resume_supported: bool = False

    with pytest.raises(UnsupportedFrame):
        replay_pending_interactions((UnknownRequest(),))


def _metrics() -> dict:
    return {
        "context_tokens_before": 1,
        "context_tokens_after_estimate": 1,
        "context_tokens_released_estimate": 0,
        "estimate_source": "heuristic",
        "history_chars_before": 1,
        "history_chars_after": 1,
        "summary_chars": 1,
        "summary_truncated": False,
        "messages_before": 1,
        "messages_after": 1,
        "messages_removed": 0,
    }
