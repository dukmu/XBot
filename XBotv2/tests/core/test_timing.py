"""Timing summaries are projections of canonical conversation records."""

import pytest

from XBotv2.core.domain import (
    CompletedStop,
    GenerationSettings,
    ModelExchange,
    ModelRoute,
    ModelTiming,
    ProviderExtensions,
    ProviderMeasured,
    RequestObservation,
    ResolvedModelSelection,
    StandardGenerationMode,
    TokenCounters,
    ToolTiming,
    TurnId,
    TurnRequest,
    UsageDelta,
)
from XBotv2.core.messages import (
    AssistantMessage,
    HumanInputMessage,
    TextPart,
    ToolMessage,
)
from XBotv2.core.domain import InputId, MessageId, ToolCallId
from XBotv2.core.tools import ToolCallRef, ToolSucceeded, text_output
from XBotv2.core.timing import SessionStats, conversation_stats


def _assistant() -> AssistantMessage:
    selection = ResolvedModelSelection(
        route=ModelRoute(provider="mock", model="test"),
        generation=GenerationSettings(
            mode=StandardGenerationMode(), max_output_tokens=128,
        ),
        context_window=4096,
    )
    observation = RequestObservation(
        selection=selection,
        purpose=TurnRequest(turn_id=TurnId("turn-1")),
        estimated_input_tokens=10,
        observed_context=ProviderMeasured(tokens=10),
    )
    return AssistantMessage(
        id=MessageId("assistant-1"),
        parts=(TextPart(text="answer"),),
        exchange=ModelExchange(
            observation=observation,
            usage=UsageDelta(counters=TokenCounters(output=25)),
            timing=ModelTiming(total_ms=1200, first_delta_ms=200),
            stop=CompletedStop(),
            provider_extensions=ProviderExtensions(provider="mock"),
        ),
    )


def test_conversation_stats_derive_turn_model_and_tool_timing():
    assistant = _assistant()
    tool = ToolMessage(
        id=MessageId("tool-1"),
        call=ToolCallRef(id=ToolCallId("call-1"), name="lookup"),
        outcome=ToolSucceeded(output=text_output("result")),
        timing=ToolTiming(duration_ms=300),
    )
    messages = (
        HumanInputMessage(
            id=MessageId("input-1"),
            input_id=InputId("input-1"),
            parts=(TextPart(text="question"),),
        ),
        assistant,
        tool,
    )

    assert conversation_stats(messages) == SessionStats(
        turns=1,
        steps=1,
        llm_ms=1200,
        tool_ms=300,
        ttft_ms=200,
        ttft_steps=1,
        decode_ms=1000,
        decode_tokens=25,
    )


def test_session_stats_adds_counters_without_mutating_inputs():
    first = SessionStats(
        turns=1,
        steps=2,
        llm_ms=12.5,
        tool_ms=4.5,
        ttft_ms=1.5,
        ttft_steps=2,
        decode_ms=8.0,
        decode_tokens=20,
    )
    second = SessionStats(
        turns=3,
        steps=4,
        llm_ms=7.5,
        tool_ms=5.5,
        ttft_ms=2.5,
        ttft_steps=3,
        decode_ms=9.0,
        decode_tokens=30,
    )

    assert first.add(second) == SessionStats(
        turns=4,
        steps=6,
        llm_ms=20.0,
        tool_ms=10.0,
        ttft_ms=4.0,
        ttft_steps=5,
        decode_ms=17.0,
        decode_tokens=50,
    )
    assert first == SessionStats(
        turns=1,
        steps=2,
        llm_ms=12.5,
        tool_ms=4.5,
        ttft_ms=1.5,
        ttft_steps=2,
        decode_ms=8.0,
        decode_tokens=20,
    )


@pytest.mark.parametrize("kwargs", [
    {"total_ms": -1},
    {"total_ms": 10, "first_delta_ms": -1},
    {"total_ms": 10, "unexpected": 1},
])
def test_model_timing_rejects_invalid_measurements(kwargs):
    with pytest.raises((ValueError, TypeError)):
        ModelTiming(**kwargs)
