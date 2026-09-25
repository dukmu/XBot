"""Tool dispatch responsiveness and timeout tests."""

from __future__ import annotations

import asyncio
import time

import pytest

from XBotv2.core.tools import Tool, ToolCall, ToolFailed
from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.agentloop.tool_runtime import execute_tools


@pytest.mark.asyncio
async def test_sync_tool_does_not_block_event_loop() -> None:
    def slow() -> str:
        time.sleep(0.3)
        return "done"

    tool = Tool.from_function(slow)
    heartbeat_finished = False

    async def heartbeat() -> None:
        nonlocal heartbeat_finished
        await asyncio.sleep(0.05)
        heartbeat_finished = True

    result, _ = await asyncio.gather(tool.ainvoke({}), heartbeat())

    assert result == "done"
    assert heartbeat_finished is True


@pytest.mark.asyncio
async def test_registered_timeout_is_reported_as_tool_error() -> None:
    def slow() -> str:
        time.sleep(0.3)
        return "late"

    registry = ToolRegistry()
    registry.register(
        Tool.from_function(slow),
        timeout_seconds=0.05,
    )

    started = time.monotonic()
    results = [
        message async for message in execute_tools(
            [ToolCall(id="call_1", name="slow", args={})],
            registry,
        )
    ]

    assert time.monotonic() - started < 0.2
    outcome = results[0].message.outcome
    assert isinstance(outcome, ToolFailed)
    assert "Tool slow timed out after 0.05s" in outcome.error.message
    assert outcome.error.code == "tool_timeout"
    assert outcome.error.message == "Tool slow timed out after 0.05s"


@pytest.mark.asyncio
async def test_invalid_tool_arguments_are_returned_to_the_model() -> None:
    invoked = False

    def choose(options: list[str]) -> str:
        nonlocal invoked
        invoked = True
        return options[0]

    registry = ToolRegistry()
    registry.register(Tool.from_function(choose))

    results = [
        message async for message in execute_tools(
            [
                ToolCall(id="call_1", name="choose", args={"options": [["nested"]]}),
                ToolCall(id="call_2", name="choose", args={"options": ["valid"], "extra": True}),
            ],
            registry,
        )
    ]

    assert invoked is False
    assert isinstance(results[0].message.outcome, ToolFailed)
    assert results[0].message.outcome.error.message == (
        "['nested'] is not of type 'string'"
    )
    assert isinstance(results[1].message.outcome, ToolFailed)
    assert "'extra' was unexpected" in results[1].message.outcome.error.message
