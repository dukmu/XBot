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
