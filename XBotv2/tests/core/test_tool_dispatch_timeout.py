"""Tool dispatch responsiveness and timeout tests."""

from __future__ import annotations

import asyncio
import time

import pytest

from XBotv2.core.tools import Tool, ToolCall
from XBotv2.tools.registry import ToolRegistry
from XBotv2.tools.runtime import execute_tools


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
        sandbox_mode="host",
        timeout_seconds=0.05,
    )

    started = time.monotonic()
    results = await execute_tools(
        [ToolCall("call_1", "slow", {})],
        registry,
    )

    assert time.monotonic() - started < 0.2
    assert results[0].status == "error"
    assert "Tool slow timed out after 0.05s" in results[0].content
    error = results[0].error
    assert error["code"] == "tool_timeout"
    assert error["message"] == "Tool slow timed out after 0.05s"
    assert error["details"] == {"timeout_seconds": 0.05}


@pytest.mark.asyncio
async def test_invalid_tool_arguments_are_returned_to_the_model() -> None:
    invoked = False

    def choose(options: list[str]) -> str:
        nonlocal invoked
        invoked = True
        return options[0]

    registry = ToolRegistry()
    registry.register(Tool.from_function(choose), sandbox_mode="host")

    results = await execute_tools([
        ToolCall("call_1", "choose", {"options": [["nested"]]}),
        ToolCall("call_2", "choose", {"options": ["valid"], "extra": True}),
    ], registry)

    assert invoked is False
    assert results[0].status == "error"
    assert results[0].content == (
        "Error: Invalid arguments for choose at options.0: "
        "['nested'] is not of type 'string'"
    )
    assert results[1].status == "error"
    assert "'extra' was unexpected" in results[1].content
