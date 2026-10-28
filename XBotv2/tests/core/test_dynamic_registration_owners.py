"""Dynamic registrations follow the lifetime of their explicit owner."""

import asyncio
import pytest
from xcore.api.context import Context
from xcore.api.errors import InactiveEffectError
from XBotv2.agentloop.impl.tool_registry import ToolRegistry
from XBotv2.agentloop.impl.tool_service import ToolsService
from XBotv2.commands.api.contracts import Command
from XBotv2.commands.impl.plugin import CommandsService
from XBotv2.core.api.tools import Tool, ToolCall, ToolSucceeded


async def test_dynamic_registration_drains_and_keeps_sibling_owner():
    root = Context()
    await root.start()
    first, second = root.scope("first"), root.scope("second")
    await first.start()
    await second.start()
    tools, commands = ToolsService(ToolRegistry()), CommandsService()

    async def sample() -> str:
        return "executed"

    tool = Tool.from_function(sample)
    command = Command(name="sample", description="sample", kind="prompt")
    async def guard(call, entry):
        return None

    tools.register(tool, owner=first)
    tools.guard(guard, owner=first)
    tools.guard(guard, owner=second)
    commands.register(command, owner=first)
    outcome = (await tools.execute_all([ToolCall(id="1", name="sample", args={})]))[0].message.outcome
    assert isinstance(outcome, ToolSucceeded)
    assert outcome.output.parts[0].text == "executed"

    # An early release followed by a replacement must not let the old owner
    # remove the replacement when its effects eventually run.
    assert tools.unregister("sample")
    assert commands.unregister("sample")
    tools.register(tool, owner=second)
    commands.register(command, owner=second)
    entered, drain = asyncio.Event(), asyncio.Event()

    async def stop():
        entered.set()
        await drain.wait()

    first.pre_stop(stop)
    closing = asyncio.create_task(first.close())
    await entered.wait()
    with pytest.raises(InactiveEffectError):
        tools.register(Tool.from_function(lambda: None, name="late"), owner=first)
    with pytest.raises(InactiveEffectError):
        commands.register(Command(name="late", description="late", kind="prompt"), owner=first)
    with pytest.raises(InactiveEffectError):
        tools.guard(guard, owner=first)
    drain.set()
    await closing
    assert tools.resolve("sample") is tool
    assert commands.get("sample") is command
    assert tools.guards() == (guard,)
    assert tools.resolve("late") is None
    await second.close()
    assert tools.resolve("sample") is None
    assert commands.get("sample") is None
    assert tools.guards() == ()
    await root.close()
