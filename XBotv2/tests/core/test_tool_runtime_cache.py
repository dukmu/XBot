"""Tool execution produces canonical messages and closed outcome variants."""

import asyncio

import pytest

from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.agentloop.tool_runtime import execute_tools
from XBotv2.core.tools import (
    GuardDecision,
    Tool,
    ToolCall,
    ToolCancelled,
    ToolDenied,
    ToolFailed,
    ToolSucceeded,
)


async def _execute(registry, call, **kwargs):
    return [item async for item in execute_tools([call], registry, **kwargs)][0]


@pytest.mark.asyncio
async def test_unregistered_and_invalid_calls_fail_at_dispatch_boundary():
    registry = ToolRegistry()
    missing = await _execute(registry, ToolCall(id="1", name="missing"))
    assert isinstance(missing.message.outcome, ToolFailed)
    assert missing.message.outcome.error.code == "not_registered"

    async def required(value: int) -> str:
        return str(value)

    registry.register(Tool.from_function(required))
    invalid = await _execute(registry, ToolCall(id="2", name="required", args={}))
    assert isinstance(invalid.message.outcome, ToolFailed)
    assert invalid.message.outcome.error.code == "invalid_arguments"


@pytest.mark.asyncio
async def test_returned_outcome_variants_are_preserved():
    for returned in (ToolDenied(reason="policy"), ToolCancelled(reason="cancelled")):
        async def probe():
            return returned

        registry = ToolRegistry()
        registry.register(Tool.from_function(probe))
        execution = await _execute(registry, ToolCall(id="1", name="probe"))
        assert execution.message.outcome == returned


@pytest.mark.asyncio
async def test_plain_return_value_is_wrapped_once_as_success():
    async def probe() -> dict[str, str]:
        return {"status": "ok"}

    registry = ToolRegistry()
    registry.register(Tool.from_function(probe))
    execution = await _execute(registry, ToolCall(id="1", name="probe"))
    assert isinstance(execution.message.outcome, ToolSucceeded)
    assert execution.message.outcome.output.parts[0].text == '{"status": "ok"}'


@pytest.mark.asyncio
async def test_guard_denial_prevents_tool_invocation():
    invoked = False

    async def probe() -> str:
        nonlocal invoked
        invoked = True
        return "done"

    async def deny(_call, _registration):
        return GuardDecision(reason="blocked", source="test")

    registry = ToolRegistry()
    registry.register(Tool.from_function(probe))
    execution = await _execute(
        registry, ToolCall(id="1", name="probe"), guards=(deny,),
    )
    assert isinstance(execution.message.outcome, ToolDenied)
    assert not invoked


@pytest.mark.asyncio
async def test_timeout_cancels_running_tool_and_returns_typed_failure():
    cancelled = asyncio.Event()

    async def probe() -> str:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    registry = ToolRegistry()
    registry.register(Tool.from_function(probe), timeout_seconds=0.01)
    execution = await _execute(registry, ToolCall(id="1", name="probe"))
    assert isinstance(execution.message.outcome, ToolFailed)
    assert execution.message.outcome.error.code == "tool_timeout"
    assert cancelled.is_set()
