from __future__ import annotations

import xcore
import pytest

from XBotv2.agentloop.engine import tool_result_event_data
from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.agentloop.tool_runtime import execute_tools
from XBotv2.core.messages import Message
from XBotv2.core.timing import (
    SESSION_STATS_METADATA_KEY,
    TIMING_METADATA_KEY,
    SessionStats,
    conversation_stats,
)
from XBotv2.core.tools import Tool, ToolCall
from XBotv2.llm.mock import MockLLM
from XBotv2.session.history import display_history
from XBotv2.tests.helpers import make_engine


@pytest.mark.asyncio
async def test_model_timing_is_persisted_and_emitted(state_store) -> None:
    engine = make_engine(
        llm=MockLLM(responses=[{"content": "done", "chunks": ["do", "ne"]}]),
        tool_registry=ToolRegistry(),
        plugin_ctx=xcore.Context(),
        state_store=state_store,
    )

    events = [event async for event in engine.run_turn("go")]
    event = next(item for item in events if item["type"] == "assistant_message")
    timing = engine.messages[-1].response_metadata[TIMING_METADATA_KEY]

    assert event["data"]["timing"] == timing
    assert timing["llm_ms"] >= timing["ttft_ms"] >= 0
    assert timing["decode_ms"] >= 0


@pytest.mark.asyncio
async def test_tool_timing_covers_success_and_dispatch_failure() -> None:
    async def ready() -> str:
        return "ok"

    registry = ToolRegistry()
    registry.register(Tool.from_function(ready, name="ready"))

    success = (await execute_tools([ToolCall("one", "ready", {})], registry))[0]
    failure = (await execute_tools([ToolCall("two", "missing", {})], registry))[0]

    for message in (success, failure):
        assert message.response_metadata[TIMING_METADATA_KEY]["duration_ms"] >= 0
        assert tool_result_event_data(message, "tool")["timing"] == (
            message.response_metadata[TIMING_METADATA_KEY]
        )


def test_conversation_stats_survive_compacted_prefix_and_replay_timing() -> None:
    assistant = Message(
        role="assistant",
        content="answer",
        response_metadata={TIMING_METADATA_KEY: {
            "llm_ms": 1200,
            "ttft_ms": 200,
            "decode_ms": 1000,
        }},
        usage_metadata={"output_tokens": 25},
    )
    tool = Message(
        role="tool",
        content="result",
        tool_call_id="call",
        response_metadata={TIMING_METADATA_KEY: {"duration_ms": 300}},
    )
    prefix = [Message(role="user", content="question"), assistant, tool]
    expected = conversation_stats(prefix)
    compacted = Message(
        role="system",
        content="summary",
        response_metadata={SESSION_STATS_METADATA_KEY: expected.to_dict()},
    )

    assert conversation_stats([compacted]) == SessionStats(
        turns=1,
        steps=1,
        llm_ms=1200,
        tool_ms=300,
        ttft_ms=200,
        ttft_steps=1,
        decode_ms=1000,
        decode_tokens=25,
    )
    assert display_history([assistant])[0]["timing"] == {
        "llm_ms": 1200,
        "ttft_ms": 200,
        "decode_ms": 1000,
    }
