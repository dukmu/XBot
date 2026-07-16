"""Tool dispatch responsiveness and timeout tests."""

from __future__ import annotations

import asyncio
import time

import pytest

from xbotv2.api.tools import Tool, ToolCall
from xbotv2.tools.registry import ToolRegistry
from xbotv2.tools.runtime import execute_tools


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
async def test_runtime_timeout_is_reported_as_tool_error(monkeypatch) -> None:
    import xbotv2.tools.runtime as runtime

    def slow() -> str:
        time.sleep(0.3)
        return "late"

    registry = ToolRegistry()
    registry.register(Tool.from_function(slow), sandbox_mode="host")
    monkeypatch.setattr(runtime, "_TOOL_DISPATCH_TIMEOUT_SECONDS", 0.05)

    started = time.monotonic()
    results = await execute_tools(
        [ToolCall("call_1", "slow", {})],
        registry,
    )

    assert time.monotonic() - started < 0.2
    assert results[0].status == "error"
    assert "Error executing slow" in results[0].content


@pytest.mark.asyncio
async def test_registered_tool_can_override_dispatch_timeout(monkeypatch) -> None:
    import xbotv2.tools.runtime as runtime

    async def slow() -> str:
        await asyncio.sleep(0.2)
        return "late"

    registry = ToolRegistry()
    registry.register(
        Tool.from_function(slow),
        sandbox_mode="host",
        timeout_seconds=0.02,
    )
    monkeypatch.setattr(runtime, "_TOOL_DISPATCH_TIMEOUT_SECONDS", 1.0)

    results = await execute_tools([ToolCall("call_1", "slow", {})], registry)

    assert results[0].status == "error"
    assert "Error executing slow" in results[0].content


@pytest.mark.asyncio
async def test_invalid_tool_arguments_are_returned_to_the_model() -> None:
    invoked = False

    def choose(options: list[str]) -> str:
        nonlocal invoked
        invoked = True
        return options[0]

    registry = ToolRegistry()
    registry.register(Tool.from_function(choose), sandbox_mode="host")

    results = await execute_tools(
        [ToolCall("call_1", "choose", {"options": [["nested"]]})],
        registry,
    )

    assert invoked is False
    assert results[0].status == "error"
    assert results[0].content == (
        "Error: Invalid arguments for choose at options.0: "
        "['nested'] is not of type 'string'"
    )
