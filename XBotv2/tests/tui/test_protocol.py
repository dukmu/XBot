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

import json
from pathlib import Path

import pytest

from XBotv2.compact.protocol import CompactionStartedData
from XBotv2.protocol import ServerEvent, server_event
from XBotv2.session.contracts import PendingInteractionData
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
    UsageUpdated,
    UserMessagePublished,
)
from XBotv2.tui.protocol import (
    FRAMES,
    IGNORED_FRAMES,
    ForeignFrame,
    FrameRejected,
    FrameTranslator,
    UnsupportedFrame,
    replay_pending_interactions,
)
from XBotv2.tui.state import SessionState, reduce

SESSION = "s1"
THREAD = "agent"

CONTRACT_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "sse" / "server_event_contracts.jsonl"
)


def translator() -> FrameTranslator:
    return FrameTranslator(session_id=SESSION, thread_id=THREAD)


def frame(frame_type: str, data: dict, **overrides) -> ServerEvent:
    base = {"session_id": SESSION, "thread_id": THREAD, "sequence": 1}
    return server_event(type=frame_type, data=data, **{**base, **overrides})


def only(frame_type: str, data: dict) -> object:
    events = translator().translate(frame(frame_type, data))
    assert len(events) == 1, f"{frame_type} produced {len(events)} events"
    return events[0]


def payload_of(frame_type: str, data: dict):
    event = only(frame_type, data)
    return getattr(event, "payload")


def contract_samples() -> list[dict]:
    return [
        json.loads(line)
        for line in CONTRACT_FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --- envelope validation --------------------------------------------------


def test_frame_for_another_session_is_rejected() -> None:
    with pytest.raises(ForeignFrame):
        translator().translate(frame("turn_started", {"turn": 1}, session_id="other"))


def test_frame_for_another_thread_is_rejected() -> None:
    with pytest.raises(ForeignFrame):
        translator().translate(frame("turn_started", {"turn": 1}, thread_id="agent-sub"))


def test_frame_without_session_or_thread_identity_is_accepted() -> None:
    events = translator().translate(
        server_event(type="turn_started", data={"turn": 2}, session_id="", thread_id="")
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


def test_turn_started_carries_the_turn_and_slots() -> None:
    events = translator().translate(
        frame("turn_started", {"turn": 3, "status_slots": {"goal": "ship"}})
    )
    assert isinstance(events[0], TurnStarted)
    assert events[0].payload.turn == 3
    assert events[1] == StatusSlotsUpdated({"goal": "ship"})


def test_turn_started_without_slots_is_only_a_turn_event() -> None:
    assert payload_of("turn_started", {"turn": 3}).turn == 3
    assert len(translator().translate(frame("turn_started", {"turn": 3}))) == 1


def test_turn_finished_carries_the_turn() -> None:
    event = only("turn_finished", {"turn": 3})
    assert isinstance(event, TurnFinished)
    assert event.payload.turn == 3


def test_turn_cancelled_carries_the_reason() -> None:
    event = only("turn_cancelled", {"turn": 3, "reason": "client_interrupt"})
    assert isinstance(event, TurnCancelled)
    assert event.payload.reason == "client_interrupt"


# --- assistant content ----------------------------------------------------


def test_assistant_delta_translates_content_and_reasoning() -> None:
    event = only("assistant_message_delta", {"content": "hi", "reasoning": "why"})
    assert isinstance(event, AssistantDelta)
    assert (event.payload.content, event.payload.reasoning) == ("hi", "why")


def test_assistant_delta_with_only_reasoning_is_kept() -> None:
    event = only("assistant_message_delta", {"reasoning": "why"})
    assert isinstance(event, AssistantDelta)
    assert event.payload.content is None


def test_assistant_message_translates_the_finished_answer() -> None:
    event = only("assistant_message", {"id": "a1", "content": "done"})
    assert isinstance(event, AssistantCompleted)
    assert event.payload.id == "a1"
    assert event.payload.content == "done"


def test_assistant_message_with_tool_calls_also_starts_them() -> None:
    """A tool-only assistant message carries no content, so its tool calls must
    still be materialized from this frame."""
    events = translator().translate(
        frame(
            "assistant_message",
            {
                "id": "a1",
                "content": "",
                "tool_calls": [{"id": "c1", "name": "bash", "args": {"command": "ls"}}],
            },
        )
    )
    assert isinstance(events[0], AssistantCompleted)
    assert isinstance(events[1], ToolCallsStarted)
    assert events[1].payload.tool_calls[0].id == "c1"
    assert events[1].payload.tool_calls[0].args == {"command": "ls"}


def test_tool_calls_started_translates_every_call() -> None:
    event = only(
        "tool_calls_started",
        {
            "tool_calls": [
                {"id": "c1", "name": "bash", "args": {}},
                {"id": "c2", "name": "read", "args": {}},
            ]
        },
    )
    assert isinstance(event, ToolCallsStarted)
    assert [call.id for call in event.payload.tool_calls] == ["c1", "c2"]


def test_tool_result_keeps_the_payload_as_sent() -> None:
    event = only(
        "tool_result",
        {"tool_call_id": "c1", "name": "bash", "status": "success", "content": {"a": 1}},
    )
    assert isinstance(event, ToolResult)
    assert event.payload.content == {"a": 1}
    assert event.payload.status == "success"


def test_tool_result_error_travels_with_the_payload() -> None:
    event = only(
        "tool_result",
        {
            "tool_call_id": "c1",
            "name": "bash",
            "status": "error",
            "content": "",
            "error": {"code": "boom", "message": "no"},
        },
    )
    assert isinstance(event, ToolResult)
    assert event.payload.error == {"code": "boom", "message": "no"}


# --- session projections --------------------------------------------------


def test_user_message_translates_to_a_published_message() -> None:
    event = only("message", {"id": "m1", "role": "user", "content": "hello"})
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
        {"agent_name": "Reviewer", "provider": "p", "model": "m", "model_mode": "fast"},
    )
    assert isinstance(event, SessionConfigured)
    assert event.payload.agent_name == "Reviewer"
    assert event.payload.model_mode == "fast"


def test_history_updated_replaces_the_timeline() -> None:
    event = only(
        "history_updated",
        {"operation": "clear", "turns": 1, "history": [{"role": "user", "content": "kept"}]},
    )
    assert isinstance(event, HistoryReplaced)
    assert [item.content for item in event.payload.history] == ["kept"]


# --- usage and compaction -------------------------------------------------


def test_usage_translates_every_reported_counter() -> None:
    event = only("usage", {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14})
    assert isinstance(event, UsageUpdated)
    assert event.payload.total_tokens == 14


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
    assert isinstance(event.payload, CompactionStartedData), "started means active"


def test_compaction_completed_carries_the_summary() -> None:
    event = only(
        "compaction_completed",
        {"reason": "manual", "automatic": False, "summary": "kept the gist", "metrics": _metrics()},
    )
    assert isinstance(event, CompactionChanged)
    assert event.payload.summary == "kept the gist"


def test_compaction_failed_carries_the_message() -> None:
    event = only("compaction_failed", {"reason": "automatic", "message": "no budget"})
    assert isinstance(event, CompactionChanged)
    assert event.payload.message == "no budget"


# --- interactions ---------------------------------------------------------


def test_permission_request_opens_a_permission_prompt() -> None:
    event = only(
        "permission_request",
        {
            "request_id": "r1",
            "source": "permission_system",
            "reason": "writes outside the workspace",
            "tool_call": {"id": "c1", "name": "bash", "args": {}},
        },
    )
    assert isinstance(event, InteractionOpened)
    assert event.request.request_id == "r1"


def test_user_input_required_opens_a_question() -> None:
    event = only(
        "user_input_required",
        {
            "request_id": "q1",
            "source": "ask_user",
            "tool_call_id": "c1",
            "question": "Which?",
            "options": [
                {"label": "A", "description": "one"},
                {"label": "B", "description": "two"},
            ],
        },
    )
    assert isinstance(event, InteractionOpened)
    assert len(event.request.options) == 2


def test_permission_response_recorded_resolves() -> None:
    event = only(
        "permission_response_recorded",
        {"request_id": "r1", "status": "answered", "decision": "allow"},
    )
    assert isinstance(event, InteractionResolved)
    assert (event.payload.request_id, event.payload.status) == ("r1", "answered")
    assert event.payload.decision == "allow"


def test_user_input_recorded_resolves() -> None:
    event = only("user_input_recorded", {"request_id": "q1", "status": "timeout"})
    assert isinstance(event, InteractionResolved)
    assert (event.payload.request_id, event.payload.status) == ("q1", "timeout")


def test_client_message_becomes_a_notice_with_its_level() -> None:
    event = only("client_message", {"message": "heads up", "level": "warning", "source": "compact"})
    assert isinstance(event, ClientNotice)
    assert event.payload.message == "heads up"
    assert event.payload.level == "warning"


def test_job_updated_translates_a_snapshot() -> None:
    event = only("job_updated", _job_payload())
    assert isinstance(event, JobUpdated)
    assert event.payload.job_id == "j1"


def test_job_completion_notice_does_not_open_a_turn() -> None:
    event = only(
        "completion_notice",
        {"type": "subagent", "kind": "subagent", "job_id": "j1", "status": "completed"},
    )
    assert isinstance(event, JobCompletionNotice)
    assert event.payload.job_id == "j1"


# --- deliberately ignored frames -----------------------------------------


@pytest.mark.parametrize("frame_type", sorted(IGNORED_FRAMES))
def test_ignored_frames_return_nothing(frame_type: str) -> None:
    """Each ignored frame is superseded by one the client does act on; the frame
    table carries the reason, and this test keeps the list honest."""
    assert translator().translate(frame(frame_type, {})) == ()


# --- the contract fixture is the completeness oracle ---------------------


@pytest.mark.parametrize("sample", contract_samples(), ids=lambda s: s["type"])
def test_every_contract_fixture_frame_is_handled_or_ignored(sample: dict) -> None:
    """The repository's own SSE contract fixture lists the frames the server
    publishes. Each must be translated or deliberately ignored -- nothing may be
    missing from both tables."""
    frame_type = sample["type"]
    assert frame_type in FRAMES or frame_type in IGNORED_FRAMES, (
        f"{frame_type} is in the SSE contract fixture but the client neither "
        "translates nor ignores it"
    )
    events = _fixture_translator(sample).translate(ServerEvent.model_validate(sample))
    state = SessionState()
    for event in events:
        reduce(state, event)


def test_a_handled_fixture_frame_really_produces_events() -> None:
    """Guards against a frame being listed but silently producing nothing."""
    generating = [sample for sample in contract_samples() if sample["type"] in FRAMES]
    assert generating, "the fixture should cover translated frames"
    for sample in generating:
        events = _fixture_translator(sample).translate(ServerEvent.model_validate(sample))
        assert events, f"{sample['type']} translated to nothing"


def _fixture_translator(sample: dict) -> FrameTranslator:
    """A translator attached to whatever session the fixture frame names."""
    return FrameTranslator(
        session_id=sample.get("session_id", ""),
        thread_id=sample.get("thread_id", ""),
    )


# --- snapshot interaction replay -----------------------------------------


def test_pending_interactions_from_a_snapshot_are_replayed() -> None:
    events = replay_pending_interactions(
        (
            PendingInteractionData(
                type="permission_request",
                data={
                    "request_id": "r1",
                    "source": "permission_system",
                    "reason": "needs approval",
                    "tool_call": {"id": "c1", "name": "bash", "args": {}},
                },
            ),
        )
    )
    assert len(events) == 1
    assert isinstance(events[0], InteractionOpened)


def test_an_unreplayable_pending_interaction_is_rejected() -> None:
    with pytest.raises(FrameRejected):
        replay_pending_interactions(
            (PendingInteractionData(type="permission_request", data={"request_id": ""}),)
        )


def test_an_unknown_pending_interaction_is_rejected() -> None:
    with pytest.raises(UnsupportedFrame):
        replay_pending_interactions((PendingInteractionData(type="mystery", data={}),))


def _job_payload() -> dict:
    return {
        "job_id": "j1",
        "kind": "agent",
        "status": "running",
        "command": "review",
        "cwd": "/w",
        "created_at": 0,
        "started_at": 1,
        "finished_at": 0,
    }


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


# --- the runtime enriches terminal turn frames ---------------------------


def test_a_terminal_turn_frame_may_carry_session_stats() -> None:
    """The session runtime adds session stats *after* the engine validated the
    frame, so the transport model must accept them. Found by the real-server
    test; without it a legitimate turn_finished was rejected as malformed."""
    payload = {
        "turn": 3,
        "status_slots": {"goal": "ship"},
        "session_stats": {"turns": 1, "steps": 2, "total_tokens": 10},
    }
    events = translator().translate(frame("turn_finished", payload))
    assert isinstance(events[0], TurnFinished)
    assert events[0].payload.turn == 3
    assert events[0].payload.session_stats["steps"] == 2
    assert events[1] == StatusSlotsUpdated({"goal": "ship"})


def test_a_cancelled_turn_frame_also_accepts_session_stats() -> None:
    events = translator().translate(
        frame(
            "turn_cancelled",
            {
                "turn": 1,
                "reason": "client_interrupt",
                "session_stats": {"turns": 1, "steps": 1},
            },
        )
    )
    assert isinstance(events[0], TurnCancelled)
    assert events[0].payload.session_stats["turns"] == 1
