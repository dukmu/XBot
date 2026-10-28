import asyncio

from XBotv2.agentloop.api.contracts import AGENT_LOOP, TOOLS, AllTools
from XBotv2.agentloop.api.state import LOOP_STATE
from XBotv2.agentloop.impl.tool_registry import ToolRegistry
from XBotv2.agentloop.impl.tool_service import ToolsService
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.tools import GuardDecision, Tool, ToolCall, ToolInvocation
from XBotv2.interactions.api.contracts import CLIENT_EVENTS
from XBotv2.permissions.api.models import PermissionRequest, Allowed
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.tests.runtime import thread_scope


async def test_tool_invocation_identity_is_canonical_and_guarded(tmp_path):
    seen = []
    guarded = []

    async def capture(value: str, *, invocation: ToolInvocation) -> str:
        seen.append((value, invocation))
        return value

    tool = Tool.from_function(capture)
    assert tool.parameters["properties"] == {"value": {"type": "string"}}
    model = MockLLM([
        {"tool_calls": [{"id": "reused", "name": "capture", "args": {"value": "first"}}]},
        {"tool_calls": [{"id": "reused", "name": "capture", "args": {"value": "second"}}]},
        {"content": "done"},
    ])
    scope = await thread_scope(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"), session_id="s", llm_override=model,
        extra_plugins=[{"id": "caption", "config": {"auto": False}},
                       {"id": "permissions", "config": {"default_decision": "allow", "rules": []}}],
    )
    try:
        tools = scope.require(TOOLS)
        tools.register(tool, cleanup="caller")
        tools.restrict(AllTools())
        def guard(call, entry):
            if entry.tool.name == "capture":
                guarded.append(call.args["value"])
                if call.args["value"] == "deny":
                    return GuardDecision(reason="denied")
        tools.guard(guard, cleanup="caller")
        loop = scope.require(AGENT_LOOP)
        await loop.send_message("run", "input")
        async with asyncio.timeout(10):
            while loop.busy:
                interactions = scope.require(CLIENT_EVENTS)
                for event in interactions.pending_interactions():
                    if isinstance(event, PermissionRequest):
                        interactions.resolve(event.interaction_id, Allowed(scope="once"))
                await asyncio.sleep(.005)
        assistant_ids = [message.id for message in scope.require(LOOP_STATE).history
                         if message.kind == "assistant" and any(isinstance(part, ToolCall) for part in message.parts)]
        assert len(assistant_ids) == 2 and assistant_ids[0] != assistant_ids[1]
        assert [(value, invocation.assistant_message_id, invocation.call_id)
                for value, invocation in seen] == [("first", assistant_ids[0], "reused"), ("second", assistant_ids[1], "reused")]
        assert guarded == ["first", "second"]
        manual_tools = ToolsService(ToolRegistry(), ownership="caller")
        manual_tools.register(tool)
        manual_tools.guard(guard)
        denied = await manual_tools.execute_all([ToolCall(id="manual", name="capture", args={"value": "deny"})])
        assert denied[0].message.outcome.kind == "denied"
        missing = await manual_tools.execute_all([ToolCall(id="manual", name="capture", args={"value": "unscoped"})])
        assert missing[0].message.outcome.kind == "failed"
        assert "requires a scoped ToolInvocation" in missing[0].message.outcome.error.message
        assert guarded == ["first", "second", "deny", "unscoped"]
        assert len(seen) == 2
    finally:
        await scope.root.close()
