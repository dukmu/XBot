"""The HTTP event envelope preserves typed scope and named payload semantics."""

import pytest
from types import SimpleNamespace
from pydantic import TypeAdapter, ValidationError

from XBotv2.agentloop.protocol import LoopEvent, LoopTurnStarted, is_loop_event
from XBotv2.core.domain import (
    AgentExecutionLimits,
    Cursor,
    GenerationSettings,
    ModelRoute,
    ReasoningGenerationMode,
    ResolvedModelSelection,
    ResolvedRuntimeSelection,
    SessionScope,
    StandardGenerationMode,
    TurnId,
    TurnScope,
)
from XBotv2.core.history import HistoryPage
from XBotv2.core.timing import SessionStats
from XBotv2.protocol.models import server_event
from XBotv2.protocol.sse import (
    SseDecodeError,
    SseDecoder,
    SseMessage,
    decode_server_event,
    encode_server_event,
)
from XBotv2.session.contracts import (
    HistoryMutation,
    OpenedThread,
    SessionEventFrame,
    SessionKey,
    ThreadMetadata,
)
from XBotv2.core.domain import UsageSnapshot
from XBotv2.session.protocol import (
    AgentConfiguredEvent,
    HistoryUpdatedEvent,
    _format_sse,
    _open_session_response,
)
from XBotv2.session.records import HumanInputRecord
from XBotv2.session.runtime import TurnEventRouter


def _decode(encoded: bytes):
    decoder = SseDecoder()
    message = None
    for line in encoded.decode("utf-8").splitlines():
        message = decoder.feed(line) or message
    assert message is not None
    return decode_server_event(message)


def _mutation(*, removed_turns: int = 0, turns: int = 0) -> HistoryMutation:
    return HistoryMutation(
        removed_turns=removed_turns,
        history=HistoryPage(items=(), older_cursor=None),
        stats=SessionStats(turns=turns),
    )


def test_session_frame_encodes_scope_kind_and_payload():
    frame = SessionEventFrame(
        sequence=7,
        scope=TurnScope(TurnId("turn-17")),
        event=HistoryUpdatedEvent(operation="undo", mutation=_mutation(removed_turns=1)),
    )

    event = _decode(_format_sse(frame, session_id="s1", thread_id="agent"))
    payload = event.model_dump(mode="json")

    assert event.kind == "history_updated"
    assert event.scope.turn_id == "turn-17"
    assert event.payload["operation"] == "undo"
    assert not {"type", "data", "request_id"}.intersection(payload)


def test_workspace_envelope_has_explicit_session_scope():
    event = server_event(
        kind="catalog/connected",
        payload={"cursor": 5},
        sequence=5,
        session_id="",
        thread_id="workspaces",
        scope=SessionScope(),
    )
    assert _decode(encode_server_event(event)) == event


def test_turn_router_uses_turn_identity_for_frame_scope():
    published = []
    runtime = SimpleNamespace(
        publish_event=lambda event, *, scope: published.append((event, scope)),
    )
    router = TurnEventRouter(runtime, TurnId("turn-17"))
    event = HistoryUpdatedEvent(operation="undo", mutation=_mutation(removed_turns=1))

    router.emit(event)

    assert published == [(event, TurnScope(TurnId("turn-17")))]


def test_malformed_or_mismatched_sse_is_rejected_not_reinterpreted():
    with pytest.raises(SseDecodeError, match="not valid JSON"):
        decode_server_event(SseMessage(event="error", data="not-json", event_id="1"))

    encoded = encode_server_event(server_event(
        kind="history_updated",
        payload={},
        sequence=1,
        session_id="s1",
        thread_id="agent",
        scope=SessionScope(),
    ))
    decoder = SseDecoder()
    message = None
    for line in encoded.decode().replace("event: history_updated", "event: wrong").splitlines():
        message = decoder.feed(line) or message
    assert message is not None
    with pytest.raises(SseDecodeError, match="does not match payload kind"):
        decode_server_event(message)


def test_loop_event_is_a_closed_discriminated_union():
    adapter = TypeAdapter(LoopEvent)
    event = adapter.validate_python({"kind": "turn_started", "turn": 2})

    assert isinstance(event, LoopTurnStarted)
    assert is_loop_event(event)
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "unknown", "turn": 2})
    assert not is_loop_event(SimpleNamespace(kind="turn_started", turn=2))


def test_history_mutation_response_carries_one_complete_page_and_stats_value():
    result = HistoryMutation(
        removed_turns=1,
        history=HistoryPage(
            items=(HumanInputRecord(id="input-1", content="remaining"),),
            older_cursor=Cursor("older"),
        ),
        stats=SessionStats(turns=1),
    )

    response = _open_session_response(
        OpenedThread(
            key=SessionKey(session_id="s1", thread_id="agent"),
            metadata=ThreadMetadata(
                workspace_root="/w",
                runtime_selection=ResolvedRuntimeSelection(
                    agent_name="agent",
                    prompt="",
                    limits=AgentExecutionLimits(),
                    enabled_tools=(),
                    model=ResolvedModelSelection(
                        route=ModelRoute(provider="openai", model="o4-mini"),
                        generation=GenerationSettings(
                            mode=StandardGenerationMode(),
                            max_output_tokens=2048,
                        ),
                        context_window=64_000,
                    ),
                ),
            ),
            usage=UsageSnapshot(),
            status_slots={},
            event_cursor=0,
            history=result.history,
            pending_inputs=(),
            pending_interactions=(),
        ),
        result.history,
    )

    assert [item.id for item in response.data.history.items] == ["input-1"]
    assert response.data.history.older_cursor == result.history.older_cursor
    assert response.data.key.session_id == "s1"
    assert response.data.key.thread_id == "agent"


def test_agent_configuration_crosses_session_event_boundary_as_one_selection():
    selection = ResolvedRuntimeSelection(
        agent_name="reviewer",
        prompt="review",
        limits=AgentExecutionLimits(max_turns=4),
        enabled_tools=("filesystem:read",),
        model=ResolvedModelSelection(
            route=ModelRoute(provider="openai", model="o4-mini"),
            generation=GenerationSettings(
                mode=ReasoningGenerationMode(effort="high"),
                max_output_tokens=2048,
            ),
            context_window=64_000,
        ),
    )
    configured = AgentConfiguredEvent(runtime_selection=selection)
    frame = SessionEventFrame(
        sequence=8,
        scope=SessionScope(),
        event=configured,
    )
    assert set(configured.model_dump()) == {"kind", "runtime_selection"}
    wire_event = _decode(_format_sse(frame, session_id="s1", thread_id="agent"))
    decoded = AgentConfiguredEvent.model_validate(
        {"kind": wire_event.kind, **wire_event.payload}
    )
    assert decoded.runtime_selection == selection
