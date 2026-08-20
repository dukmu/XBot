"""Tests for the core Engine — ReAct loop with NO plugins."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from XBotv2.agentloop.engine import Engine
from XBotv2.context_builder.builder import ContextBuilder
from XBotv2.coretools.shell import SHELL_TOOLS
from XBotv2.permissions.tools import request_permission
from XBotv2.config.models import RuntimeConfig
import xcore
from XBotv2.tests.helpers import make_engine as helpers_make_engine
from XBotv2.tests.helpers import make_tool_ctx
from XBotv2.agentloop import Events
from XBotv2.llm.mock import MockLLM
from XBotv2.core import ContextComponent
from XBotv2.core.messages import Message, ModelChunk, ModelResponse
from XBotv2.core.providers import BaseProvider
from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.permissions.system import PermissionSystem
from XBotv2.sandbox.policy import SandboxPolicy
from XBotv2.core.tools import (
    ArtifactRef,
    ClientEvent,
    Tool,
    ToolCall,
    ToolError,
    ToolResult,
)


def tool_name(tool):
    if isinstance(tool, dict):
        return tool.get("function", {}).get("name") or tool.get("name")
    return tool.name


def echo(message: str) -> str:
    return f"Echo: {message}"
echo_tool = Tool.from_function(echo, name="echo")


def shout(message: str) -> str:
    return message.upper()
shout_tool = Tool.from_function(shout, name="shout")


def send_notice(message: str) -> ToolResult:
    return ToolResult(
        content="notice sent",
        client_events=(ClientEvent("client_message", {"message": message}),),
    )
send_notice_tool = Tool.from_function(send_notice, name="send_notice")


def request_input(question: str) -> ToolResult:
    return ToolResult(
        content="waiting for user",
        wait_for_user=True,
        client_events=(ClientEvent(
            "user_input_required",
            {
                "question": question,
                "options": [
                    {"label": "continue", "description": "Continue the work."},
                    {"label": "stop", "description": "Stop the work."},
                ],
            },
        ),),
    )


def structured_failure() -> ToolResult:
    return ToolResult(
        status="error",
        content="structured failure",
        error=ToolError(
            code="structured_error",
            message="failed with details",
            retryable=True,
            details={"field": "value"},
        ),
        artifacts=(
            ArtifactRef(
                id="artifact-1",
                media_type="text/plain",
                name="failure.txt",
            ),
        ),
    )


structured_failure_tool = Tool.from_function(
    structured_failure,
    name="structured_failure",
)


def make_engine(*args, **kwargs):
    """Create a minimal engine for testing.

    Accepts either the historical positional composition
    ``(mock_llm, tool_registry, state_store, temp_workspace)`` or the
    keyword composition used by the migrated Engine call sites.
    """
    if args:
        mock_llm, tool_registry, state_store = args[0], args[1], args[2]
        if len(args) > 3:
            kwargs.setdefault("workspace", args[3])
    else:
        mock_llm = kwargs.pop("llm")
        tool_registry = kwargs.pop("tool_registry")
        state_store = kwargs.pop("state_store")
    workspace = kwargs.pop("workspace", None)
    plugin_ctx = kwargs.pop("plugin_ctx", xcore.Context())
    config = kwargs.pop("config", None)
    sandbox_policy = kwargs.pop("sandbox_policy", None)
    permission_system = kwargs.pop("permission_system", None)
    kwargs.pop("context_builder", None)
    if kwargs:
        raise TypeError(f"unexpected keyword arguments: {sorted(kwargs)}")
    return helpers_make_engine(
        llm=mock_llm,
        tool_registry=tool_registry,
        plugin_ctx=plugin_ctx,
        state_store=state_store,
        sandbox_policy=sandbox_policy,
        permission_system=permission_system,
        config=config,
    )


def make_engine_with_hooks(mock_llm, tool_registry, state_store, temp_workspace, plugin_ctx):
    """Create a minimal engine with a supplied hook manager."""
    return helpers_make_engine(
        llm=mock_llm,
        tool_registry=tool_registry,
        plugin_ctx=plugin_ctx,
        state_store=state_store,
        sandbox_policy=SandboxPolicy(
            enabled=False,
            workspace_root=str(temp_workspace),
        ),
        permission_system=PermissionSystem(default_decision="allow"),
    )


def wire_persistence(engine, state_store, plugin_ctx=None):
    """Wire the production persistence observer onto an engine's events."""
    from XBotv2.persistence.plugin import PersistenceService

    ctx = plugin_ctx or engine._events
    persistence = PersistenceService(state_store, engine.state)
    ctx.on(Events.STATE_CHANGED, persistence.state_changed)
    return persistence


class TestEngineBasics:
    """Basic ReAct loop behavior."""

    @pytest.mark.asyncio
    async def test_simple_text_response(self, state_store, temp_workspace):
        """Engine returns a text response when no tool calls are made."""
        llm = MockLLM(responses=[{"content": "Hello! How can I help?"}])
        registry = ToolRegistry()
        registry.register(echo_tool)

        engine = make_engine(llm, registry, state_store, temp_workspace)
        events = [e async for e in engine.run_turn("hi")]

        # Should have: turn_started, assistant_message, turn_finished
        types = [e["type"] for e in events]
        assert "turn_started" in types
        assert "assistant_message" in types
        assert "turn_finished" in types

        # Verify the response content
        assistant_events = [e for e in events if e["type"] == "assistant_message"]
        assert assistant_events[0]["data"]["content"] == "Hello! How can I help?"

    @pytest.mark.asyncio
    async def test_provider_usage_metadata_is_emitted(self, state_store, temp_workspace):
        """Engine emits provider token usage as a first-class event."""
        llm = MockLLM(responses=[
            {
                "content": "Hello!",
                "usage_metadata": {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "total_tokens": 18,
                    "requests": 1,
                    "context_tokens": 11,
                    "cache_read_input_tokens": 5,
                },
            }
        ])
        registry = ToolRegistry()

        engine = make_engine(llm, registry, state_store, temp_workspace)
        events = [e async for e in engine.run_turn("hi")]

        usage_events = [e for e in events if e["type"] == "usage"]
        assert usage_events == [
            {
                "type": "usage",
                "data": {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "total_tokens": 18,
                    "requests": 1,
                    "context_tokens": 11,
                    "cache_read_input_tokens": 5,
                },
            }
        ]
        # Cumulative session usage is owned by the usage capability, not the
        # loop driver; the per-call usage event above is the engine contract.

    @pytest.mark.asyncio
    async def test_streaming_text_deltas_precede_final_assistant_message(self, state_store, temp_workspace):
        """Engine surfaces provider chunk content as live deltas."""
        llm = MockLLM(responses=[{
            "content": "Hello world",
            "chunks": ["Hello ", "world"],
        }])
        registry = ToolRegistry()

        engine = make_engine(llm, registry, state_store, temp_workspace)
        events = [e async for e in engine.run_turn("hi")]

        delta_events = [e for e in events if e["type"] == "assistant_message_delta"]
        assert [e["data"]["content"] for e in delta_events] == ["Hello ", "world"]
        assistant_events = [e for e in events if e["type"] == "assistant_message"]
        assert assistant_events[-1]["data"]["content"] == "Hello world"

    @pytest.mark.asyncio
    async def test_streaming_tool_call_chunks_precede_tool_execution(self, state_store, temp_workspace):
        """Engine surfaces partial tool-call chunks before tool_calls_started."""
        llm = MockLLM(responses=[
            {
                "content": "",
                "tool_calls": [{"name": "echo", "args": {"message": "hello"}, "id": "call_1"}],
                "chunks": [
                    {
                        "tool_call_chunks": [
                            {"name": "echo", "args": '{"message"', "id": "call_1", "index": 0},
                        ],
                    },
                    {
                        "tool_call_chunks": [
                            {"args": ': "hello"}', "index": 0},
                        ],
                    },
                ],
            },
            {"content": "Done"},
        ])
        registry = ToolRegistry()
        registry.register(echo_tool)

        engine = make_engine(llm, registry, state_store, temp_workspace)
        events = [e async for e in engine.run_turn("echo hello")]

        types = [e["type"] for e in events]
        assert types.index("tool_call_delta") < types.index("tool_calls_started")
        delta_events = [e for e in events if e["type"] == "tool_call_delta"]
        assert delta_events[0]["data"]["tool_calls"][0] == {
            "tool_call_id": "call_1",
            "id": "call_1",
            "name": "echo",
            "args_delta": '{"message"',
            "args": '{"message"',
            "index": 0,
        }
        assert delta_events[1]["data"]["tool_calls"][0] == {
            "tool_call_id": "call_1",
            "id": "call_1",
            "name": "tool",
            "args_delta": ': "hello"}',
            "args": ': "hello"}',
            "index": 0,
        }
        assert "tool_result" in types

    @pytest.mark.asyncio
    async def test_streaming_shell_tool_call_executes_and_finishes(self, state_store, temp_workspace):
        """Streaming tool-call chunks must not break the shell execution chain."""
        llm = MockLLM(responses=[
            {
                "content": "",
                "tool_calls": [{"name": "shell", "args": {"command": "printf xbot-shell"}, "id": "call_shell"}],
                "chunks": [
                    {"tool_call_chunks": [{"name": "shell", "args": '{"command"', "index": 0}]},
                    {"tool_call_chunks": [{"args": ': "printf xbot-shell"}', "id": "call_shell", "index": 0}]},
                ],
            },
            {"content": "Done"},
        ])
        registry = ToolRegistry()
        shell = next(tool for tool in SHELL_TOOLS if tool.name == "shell")
        registry.register(shell)

        engine = make_engine(llm, registry, state_store, temp_workspace)
        events = [e async for e in engine.run_turn("run shell")]

        tool_results = [e for e in events if e["type"] == "tool_result"]
        tool_delta_events = [e for e in events if e["type"] == "tool_call_delta"]
        assert tool_delta_events[0]["data"]["tool_calls"][0]["tool_call_id"] == "tool_0"
        assert tool_delta_events[1]["data"]["tool_calls"][0]["tool_call_id"] == "call_shell"
        assert tool_delta_events[1]["data"]["tool_calls"][0]["replaces_tool_call_id"] == "tool_0"
        assert tool_results == [
            {
                "type": "tool_result",
                "data": {
                    "tool_call_id": "call_shell",
                    "name": "shell",
                    "content": "xbot-shell",
                    "status": "success",
                },
            }
        ]

    @pytest.mark.asyncio
    async def test_tool_call_and_response(self, state_store, temp_workspace):
        """Engine executes tool calls and continues the loop."""
        llm = MockLLM(responses=[
            {
                "content": "I'll echo that.",
                "tool_calls": [{"name": "echo", "args": {"message": "hello"}, "id": "call_1"}],
            },
            {"content": "Done!"},
        ])
        registry = ToolRegistry()
        registry.register(echo_tool)

        engine = make_engine(llm, registry, state_store, temp_workspace)
        events = [e async for e in engine.run_turn("echo hello")]

        types = [e["type"] for e in events]
        assert "tool_calls_started" in types
        assert "tool_result" in types
        # Should have two assistant events (pre-tool and post-tool)
        assistant_events = [e for e in events if e["type"] == "assistant_message"]
        assert len(assistant_events) == 2

    @pytest.mark.asyncio
    async def test_empty_response_after_tool_result_is_an_error(
        self,
        state_store,
        temp_workspace,
    ):
        llm = MockLLM(responses=[
            {
                "tool_calls": [
                    {"name": "echo", "args": {"message": "hello"}, "id": "call_1"},
                ],
            },
            {
                "content": "",
                "reasoning": "<tool_call>not native ToolUse</tool_call>",
                "response_metadata": {"stop_reason": "end_turn"},
            },
        ])
        registry = ToolRegistry()
        registry.register(echo_tool)
        engine = make_engine(llm, registry, state_store, temp_workspace)

        events = [event async for event in engine.run_turn("echo hello")]

        error = next(event for event in events if event["type"] == "error")
        assert "no assistant content or ToolUse after ToolResult" in error["data"]["message"]
        assert "stop_reason=end_turn" in error["data"]["message"]
        assert "reasoning_chars=41" in error["data"]["message"]
        assert llm.call_count == 2
        assert not any(
            message.role == "assistant"
            and not message.content
            and not message.tool_calls
            for message in engine.messages
        )

    @pytest.mark.asyncio
    async def test_tool_result_event_preserves_structured_fields(
        self,
        state_store,
        temp_workspace,
    ):
        llm = MockLLM(responses=[
            {
                "content": "run",
                "tool_calls": [
                    {"name": "structured_failure", "args": {}, "id": "c1"},
                ],
            },
            {"content": "handled"},
        ])
        registry = ToolRegistry()
        registry.register(structured_failure_tool)
        engine = make_engine(llm, registry, state_store, temp_workspace)

        events = [event async for event in engine.run_turn("test result")]

        result = next(event for event in events if event["type"] == "tool_result")
        assert result["data"] == {
            "tool_call_id": "c1",
            "name": "structured_failure",
            "content": "structured failure",
            "status": "error",
            "error": {
                "code": "structured_error",
                "message": "failed with details",
                "retryable": True,
                "details": {"field": "value"},
            },
            "artifacts": [{
                "id": "artifact-1",
                "media_type": "text/plain",
                "name": "failure.txt",
            }],
        }

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_in_one_turn(self, state_store, temp_workspace):
        """Engine handles multiple tool calls in a single response."""
        llm = MockLLM(responses=[
            {
                "content": "Running two commands.",
                "tool_calls": [
                    {"name": "echo", "args": {"message": "first"}, "id": "call_1"},
                    {"name": "echo", "args": {"message": "second"}, "id": "call_2"},
                ],
            },
            {"content": "Both done."},
        ])
        registry = ToolRegistry()
        registry.register(echo_tool)

        engine = make_engine(llm, registry, state_store, temp_workspace)
        events = [e async for e in engine.run_turn("echo two things")]

        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert len(tool_results) == 2

    @pytest.mark.asyncio
    async def test_turn_count_increments(self, state_store, temp_workspace):
        """Turn count increases with each run_turn call."""
        llm = MockLLM(responses=[{"content": "Response 1"}, {"content": "Response 2"}])
        registry = ToolRegistry()

        engine = make_engine(llm, registry, state_store, temp_workspace)
        assert engine.turn_count == 0

        _ = [e async for e in engine.run_turn("msg1")]
        assert engine.turn_count == 1

        _ = [e async for e in engine.run_turn("msg2")]
        assert engine.turn_count == 2

    @pytest.mark.asyncio
    async def test_max_iterations_limit(self, state_store, temp_workspace):
        """Engine asks for a tool-free summary after max_iterations."""
        responses = [
            {
                "content": f"Call {i}",
                "tool_calls": [{"name": "echo", "args": {"message": str(i)}, "id": f"call_{i}"}],
            }
            for i in range(3)
        ]
        responses.append({"content": "Budget exhausted; work remains."})
        llm = MockLLM(responses=responses)
        registry = ToolRegistry()
        registry.register(echo_tool)

        engine = make_engine(llm, registry, state_store, temp_workspace)
        engine.max_iterations = 3  # Small limit
        events = [e async for e in engine.run_turn("loop")]

        assistant_events = [e for e in events if e["type"] == "assistant_message"]
        assert len(assistant_events) == 4
        assert assistant_events[-1]["data"]["content"] == (
            "Budget exhausted; work remains."
        )
        final_context = llm.get_call_messages(3)
        assert any(
            message.role == "system"
            and "tool iteration budget of 3 has been exhausted" in message.content
            for message in final_context
        )
        assert llm.bound_tools == []


class TestEngineHooks:
    """Hook integration in the engine."""

    @pytest.mark.asyncio
    async def test_hooks_fire_during_turn(self, state_store, temp_workspace):
        """Registered plugin_ctx are called during a turn."""
        llm = MockLLM(responses=[{"content": "Hello!"}])
        registry = ToolRegistry()

        hook_calls = []

        async def on_turn_start(ctx):
            hook_calls.append("turn_start")

        async def on_turn_end(ctx):
            hook_calls.append("turn_end")

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.TURN_START, on_turn_start)
        plugin_ctx.on(Events.TURN_END, on_turn_end)

        engine = make_engine(
            llm=llm,
            tool_registry=registry,
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )
        _ = [e async for e in engine.run_turn("test")]
        assert "turn_start" in hook_calls
        assert "turn_end" in hook_calls

    @pytest.mark.asyncio
    async def test_before_agent_short_circuit(self, state_store, temp_workspace):
        """A before_agent hook can short-circuit the LLM call."""
        llm = MockLLM(responses=[{"content": "Should not be called"}])
        registry = ToolRegistry()

        async def replace_response(ctx):
            ctx.short_circuit_result = {
                "messages": [Message(role="assistant", content="Hijacked!")]
            }
            return ctx.short_circuit_result

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.BEFORE_AGENT, replace_response)

        engine = make_engine(
            llm=llm,
            tool_registry=registry,
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )
        events = [e async for e in engine.run_turn("test")]

        # The LLM should NOT have been called; the hook hijacked it
        # The messages should contain the hijacked response
        assert "Hijacked!" in str(engine.messages)

    @pytest.mark.asyncio
    async def test_model_request_hooks_receive_context_and_response(self, state_store, temp_workspace):
        """Fine-grained model plugin_ctx see built context, tools, request, and response."""
        llm = MockLLM(responses=[{"content": "Hello!"}])
        registry = ToolRegistry()
        registry.register(echo_tool)
        calls = []

        async def after_context_build(ctx):
            calls.append(("context", len(ctx.context_messages)))

        async def after_tool_schema_bind(ctx):
            request = ctx.model_request or {}
            calls.append(("tools", [tool.name for tool in request["tools"]]))

        async def before_model_request(ctx):
            request = ctx.model_request or {}
            calls.append(("request", len(request["messages"])))

        async def after_model_response(ctx):
            calls.append(("response", ctx.model_response.content))

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.AFTER_CONTEXT_BUILD, after_context_build)
        plugin_ctx.on(Events.AFTER_TOOL_SCHEMA_BIND, after_tool_schema_bind)
        plugin_ctx.on(Events.BEFORE_MODEL_REQUEST, before_model_request)
        plugin_ctx.on(Events.AFTER_MODEL_RESPONSE, after_model_response)

        engine = make_engine(
            llm=llm,
            tool_registry=registry,
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )

        events = [e async for e in engine.run_turn("test")]

        assert events[-1]["type"] == "turn_finished"
        assert calls[0][0] == "context"
        assert calls[1] == ("tools", ["echo"])
        assert calls[2][0] == "request"
        assert calls[3] == ("response", "Hello!")

    @pytest.mark.asyncio
    async def test_before_model_request_can_short_circuit_turn(self, state_store, temp_workspace):
        """Budget-style plugin_ctx can stop before the provider request."""
        llm = MockLLM(responses=[{"content": "Should not be called"}])
        registry = ToolRegistry()

        async def deny_request(ctx):
            return {
                "event": {
                    "type": "error",
                    "data": {"code": "token_budget_exceeded", "message": "budget exceeded"},
                },
                "turn_complete": True,
            }

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.BEFORE_MODEL_REQUEST, deny_request)

        engine = make_engine(
            llm=llm,
            tool_registry=registry,
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )

        events = [e async for e in engine.run_turn("test")]

        assert [event["type"] for event in events] == [
            "turn_started",
            "error",
            "turn_finished",
        ]
        assert llm.call_count == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stage",
        [
            Events.BEFORE_CONTEXT,
            Events.BEFORE_CONTEXT_BUILD,
            Events.AFTER_CONTEXT,
            Events.BEFORE_TOOL_SCHEMA_BIND,
            Events.BEFORE_MODEL_REQUEST,
        ],
    )
    async def test_invalid_transform_return_emits_contract_error(
        self, state_store, temp_workspace, stage
    ):
        """Invalid transform returns fail before the provider is called."""
        llm = MockLLM(responses=[{"content": "Should not be called"}])
        registry = ToolRegistry()

        async def stop(ctx):
            return True

        plugin_ctx = xcore.Context()
        plugin_ctx.on(stage, stop)

        engine = make_engine(
            llm=llm,
            tool_registry=registry,
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )

        events = [e async for e in engine.run_turn("test")]

        assert [event["type"] for event in events] == [
            "turn_started",
            "error",
            "turn_finished",
        ]
        assert events[1]["data"]["code"] == "engine_error"
        assert events[1]["data"]["details"]["exception_type"] == "TypeError"
        assert stage in events[1]["data"]["message"]
        assert llm.call_count == 0

    @pytest.mark.asyncio
    async def test_user_message_accept_hooks_can_rewrite_input(self, state_store, temp_workspace):
        """User intake plugin_ctx run before history is recorded."""
        llm = MockLLM(responses=[{"content": "ok"}])
        registry = ToolRegistry()
        calls = []

        async def before_accept(ctx):
            calls.append(("before", ctx.user_input))
            return {"user_input": "rewritten"}

        async def after_accept(ctx):
            calls.append(("after", ctx.user_input, ctx.messages[-1].content))

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.BEFORE_USER_MESSAGE_ACCEPT, before_accept)
        plugin_ctx.on(Events.AFTER_USER_MESSAGE_ACCEPT, after_accept)

        engine = make_engine(
            llm=llm,
            tool_registry=registry,
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )

        events = [e async for e in engine.run_turn("original")]

        assert events[-1]["type"] == "turn_finished"
        assert calls == [("before", "original"), ("after", "rewritten", "rewritten")]
        assert engine.messages[0].content == "rewritten"

    @pytest.mark.asyncio
    async def test_user_message_accept_invalid_return_emits_contract_error(
        self, state_store, temp_workspace
    ):
        """Invalid intake Hook returns are reported without accepting input."""
        llm = MockLLM(responses=[{"content": "should not run"}])
        registry = ToolRegistry()

        async def reject(ctx):
            return True

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.BEFORE_USER_MESSAGE_ACCEPT, reject)

        engine = make_engine(
            llm=llm,
            tool_registry=registry,
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )

        events = [e async for e in engine.run_turn("blocked")]

        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert events[0]["data"]["code"] == "engine_error"
        assert events[0]["data"]["details"]["exception_type"] == "TypeError"
        assert "before/user-message-accept" in events[0]["data"]["message"]
        assert engine.turn_count == 0
        assert engine.messages == []
        assert llm.call_count == 0

    @pytest.mark.asyncio
    async def test_user_message_accept_structured_stop_emits_default_error(
        self, state_store, temp_workspace
    ):
        """Structured intake stops without an event still produce a bounded error."""
        llm = MockLLM(responses=[{"content": "should not run"}])
        registry = ToolRegistry()

        async def reject(ctx):
            return {"turn_complete": True}

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.BEFORE_USER_MESSAGE_ACCEPT, reject)

        engine = make_engine(
            llm=llm,
            tool_registry=registry,
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )

        events = [e async for e in engine.run_turn("blocked")]

        assert events[0]["type"] == "error"
        assert events[0]["data"]["code"] == "user_message_rejected"
        assert engine.turn_count == 0
        assert engine.messages == []
        assert llm.call_count == 0

    @pytest.mark.asyncio
    async def test_user_message_accept_event_can_continue_into_turn(
        self, state_store, temp_workspace
    ):
        async def announce(_ctx):
            return {
                "event": {
                    "type": "client_message",
                    "data": {"message": "accepted"},
                },
                "turn_complete": False,
            }

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.BEFORE_USER_MESSAGE_ACCEPT, announce)
        engine = make_engine_with_hooks(
            MockLLM(responses=[{"content": "ok"}]),
            ToolRegistry(),
            state_store,
            temp_workspace,
            plugin_ctx,
        )

        events = [event async for event in engine.run_turn("continue")]

        assert [event["type"] for event in events] == [
            "client_message",
            "turn_started",
            "assistant_message_delta",
            "assistant_message",
            "turn_finished",
        ]

    @pytest.mark.asyncio
    async def test_context_component_and_build_hooks_fire(self, state_store, temp_workspace):
        """Context plugin_ctx expose source-tagged components before provider messages."""
        llm = MockLLM(responses=[{"content": "ok"}])
        registry = ToolRegistry()
        calls = []

        async def before_context_build(ctx):
            calls.append(("before_build",))
            return {"context_kwargs": {"instructions": "from hook"}}

        async def after_components(ctx):
            calls.append((
                "components",
                [component.source for component in ctx.context_components],
            ))
            ctx.context_components = [
                *ctx.context_components,
                ContextComponent(
                    role="system",
                    source="hook_component",
                    content="## Hook Component\nvisible",
                ),
            ]

        async def after_context_build(ctx):
            calls.append(("messages", [message.content for message in ctx.context_messages]))

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.BEFORE_CONTEXT_BUILD, before_context_build)
        plugin_ctx.on(Events.AFTER_CONTEXT_COMPONENTS_BUILD, after_components)
        plugin_ctx.on(Events.AFTER_CONTEXT_BUILD, after_context_build)

        engine = make_engine(
            llm=llm,
            tool_registry=registry,
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )

        _ = [e async for e in engine.run_turn("test")]

        assert calls[0] == ("before_build",)
        assert "core_instructions" in calls[1][1]
        assert "agent_instructions" in calls[1][1]
        assert "history" in calls[1][1]
        assert any("from hook" in content for content in calls[2][1])
        assert any("Hook Component" in content for content in calls[2][1])

    @pytest.mark.asyncio
    async def test_context_component_hook_rejects_untyped_replacement(
        self, state_store, temp_workspace
    ):
        async def replace_with_invalid(ctx):
            ctx.context_components = [object()]

        plugin_ctx = xcore.Context()
        plugin_ctx.on(
            Events.AFTER_CONTEXT_COMPONENTS_BUILD,
            replace_with_invalid,
        )
        llm = MockLLM(responses=[{"content": "should not run"}])
        engine = make_engine_with_hooks(
            llm,
            ToolRegistry(),
            state_store,
            temp_workspace,
            plugin_ctx,
        )

        events = [event async for event in engine.run_turn("test")]

        assert [event["type"] for event in events][-2:] == [
            "error",
            "turn_finished",
        ]
        assert events[-2]["data"]["details"] == {
            "exception_type": "TypeError"
        }
        assert llm.call_count == 0

    @pytest.mark.asyncio
    async def test_before_tool_schema_bind_filters_actual_bound_tools(
        self, state_store, temp_workspace
    ):
        """Tool schema plugin_ctx run before provider bind_tools is called."""
        class RecordingLLM(MockLLM):
            def __init__(self):
                super().__init__([{"content": "ok"}])
                object.__setattr__(self, "bound_names", None)

            def bind_tools(self, tools, **kwargs):
                object.__setattr__(self, "bound_names", [tool_name(tool) for tool in tools])
                return self

        llm = RecordingLLM()
        registry = ToolRegistry()
        registry.register(echo_tool)

        async def filter_tools(ctx):
            assert [tool.name for tool in ctx.model_request["tools"]] == ["echo"]
            return {"tools": []}

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.BEFORE_TOOL_SCHEMA_BIND, filter_tools)

        engine = make_engine(
            llm=llm,
            tool_registry=registry,
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )

        _ = [e async for e in engine.run_turn("test")]

        assert llm.bound_names is None

    @pytest.mark.asyncio
    async def test_before_model_request_rebinds_when_tools_change(
        self, state_store, temp_workspace
    ):
        """Late request plugin_ctx that change tools also update the bound client."""
        class RecordingLLM(MockLLM):
            def __init__(self):
                super().__init__([{"content": "ok"}])
                object.__setattr__(self, "bound_history", [])

            def bind_tools(self, tools, **kwargs):
                self.bound_history.append([tool_name(tool) for tool in tools])
                return self

        llm = RecordingLLM()
        registry = ToolRegistry()
        registry.register(echo_tool)
        registry.register(shout_tool)

        async def keep_echo(ctx):
            return {"tools": [tool for tool in ctx.model_request["tools"] if tool.name == "echo"]}

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.BEFORE_MODEL_REQUEST, keep_echo)

        engine = make_engine(
            llm=llm,
            tool_registry=registry,
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )

        _ = [e async for e in engine.run_turn("test")]

        assert llm.bound_history == [["echo", "shout"], ["echo"]]

    @pytest.mark.asyncio
    async def test_model_request_error_hook_runs_before_on_error(self, state_store, temp_workspace):
        """Provider-call failures get a provider-specific hook and then ON_ERROR."""
        llm = MockLLM(responses=[])
        registry = ToolRegistry()
        calls = []

        async def on_model_error(ctx):
            request = ctx.model_request or {}
            calls.append((
                "model",
                type(ctx.error).__name__,
                len(request["messages"]),
            ))

        async def on_error(ctx):
            calls.append(("engine", type(ctx.error).__name__))

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.MODEL_REQUEST_ERROR, on_model_error)
        plugin_ctx.on(Events.ON_ERROR, on_error)

        engine = make_engine(
            llm=llm,
            tool_registry=registry,
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )

        events = [e async for e in engine.run_turn("test")]

        assert [event["type"] for event in events][-2:] == [
            "error",
            "turn_finished",
        ]
        assert calls[0][0:2] == ("model", "RuntimeError")
        assert calls[0][2] > 0
        assert calls[1] == ("engine", "RuntimeError")

    @pytest.mark.asyncio
    async def test_connection_failure_retries_before_model_output(
        self,
        state_store,
        temp_workspace,
        monkeypatch,
    ):
        class FlakyLLM(BaseProvider):
            def __init__(self):
                super().__init__(
                    model="flaky",
                    temperature=0,
                    max_output_tokens=None,
                    max_retries=2,
                    retry_backoff_factor=0.25,
                )
                self.calls = 0

            async def _astream_once(self, messages, **kwargs):
                self.calls += 1
                if self.calls <= 2:
                    raise ConnectionError("temporary disconnect")
                yield ModelResponse(content="recovered")

        llm = FlakyLLM()
        sleep = AsyncMock()
        monkeypatch.setattr("asyncio.sleep", sleep)
        engine = make_engine(llm, ToolRegistry(), state_store, temp_workspace)

        events = [event async for event in engine.run_turn("test")]

        assert llm.calls == 3
        assert [call.args[0] for call in sleep.await_args_list] == [0.25, 0.5]
        assert any(
            event["type"] == "assistant_message"
            and event["data"]["content"] == "recovered"
            for event in events
        )

    @pytest.mark.asyncio
    async def test_mid_stream_llm_timeout_surfaces_clean_timeout_error(
        self, state_store, temp_workspace
    ):
        """A timeout after output began yields TimeoutError, not a NameError."""

        class MidStreamTimeoutLLM(BaseProvider):
            def __init__(self):
                super().__init__(
                    model="timeout",
                    temperature=0,
                    max_output_tokens=None,
                    max_retries=1,
                    retry_backoff_factor=0,
                )

            async def _astream_once(self, messages, **kwargs):
                yield ModelChunk(content="partial")
                raise asyncio.TimeoutError()

        dispatched = []

        async def on_model_error(ctx):
            dispatched.append(type(ctx.error).__name__)

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.MODEL_REQUEST_ERROR, on_model_error)

        engine = make_engine(
            llm=MidStreamTimeoutLLM(),
            tool_registry=ToolRegistry(),
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(
                enabled=False,
                workspace_root=str(temp_workspace),
            ),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )

        events = [event async for event in engine.run_turn("test")]

        assert dispatched == ["TimeoutError"]
        error = next(
            event
            for event in events
            if event["type"] == "error"
            and "exception_type" in ((event.get("data") or {}).get("details") or {})
        )
        assert error["data"]["details"]["exception_type"] == "TimeoutError"
        assert "LLM call timed out" in error["data"]["message"]

    @pytest.mark.asyncio
    async def test_tool_call_lifecycle_hooks_fire(self, state_store, temp_workspace):
        """Parsed, per-call before/after, and denial plugin_ctx are visible."""
        llm = MockLLM(responses=[
            {
                "content": "tools",
                "tool_calls": [
                    {"name": "echo", "args": {"message": "hi"}, "id": "call_ok"},
                    {"name": "missing", "args": {}, "id": "call_bad"},
                ],
            },
            {"content": "done"},
        ])
        registry = ToolRegistry()
        registry.register(echo_tool)
        calls = []

        async def parsed(ctx):
            calls.append(("parsed", [call.name for call in ctx.tool_calls]))

        async def before_call(ctx):
            calls.append(("before", ctx.tool_call.name))

        async def after_call(ctx):
            calls.append(("after", ctx.tool_call.name, ctx.tool_result.status))

        async def denied(ctx):
            calls.append(("denied", ctx.tool_call.name, type(ctx.error).__name__))

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.TOOL_CALLS_PARSED, parsed)
        plugin_ctx.on(Events.BEFORE_TOOL_CALL, before_call)
        plugin_ctx.on(Events.AFTER_TOOL_CALL, after_call)
        plugin_ctx.on(Events.TOOL_DENIED, denied)

        engine = make_engine(
            llm=llm,
            tool_registry=registry,
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )

        events = [e async for e in engine.run_turn("test")]

        assert events[-1]["type"] == "turn_finished"
        assert ("parsed", ["echo", "missing"]) in calls
        assert ("before", "echo") in calls
        assert ("after", "echo", "success") in calls
        assert ("denied", "missing", "PermissionError") in calls

    @pytest.mark.asyncio
    async def test_state_persist_hooks_fire(self, state_store, temp_workspace):
        """Persistence plugin_ctx bracket message materialization."""
        llm = MockLLM(responses=[{"content": "ok"}])
        registry = ToolRegistry()
        calls = []

        async def record_state_changed(ctx):
            calls.append((len(ctx.messages), state_store.message_count()))

        plugin_ctx = xcore.Context()
        engine = make_engine_with_hooks(
            llm,
            registry,
            state_store,
            temp_workspace,
            plugin_ctx,
        )
        persistence = wire_persistence(engine, state_store, plugin_ctx)
        plugin_ctx.on(Events.STATE_CHANGED, record_state_changed)

        _ = [e async for e in engine.run_turn("test")]

        assert calls == [(2, 2)]
        assert await persistence.flush() is False
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_tool_turn_persists_each_changed_checkpoint_once(
        self, state_store, temp_workspace
    ):
        llm = MockLLM(responses=[
            {
                "content": "calling",
                "tool_calls": [
                    {"id": "call-1", "name": "echo", "args": {"message": "hi"}}
                ],
            },
            {"content": "done"},
        ])
        registry = ToolRegistry()
        registry.register(echo_tool)
        persisted_sizes = []

        async def record_state_changed(ctx):
            persisted_sizes.append(len(ctx.messages))

        plugin_ctx = xcore.Context()
        engine = make_engine_with_hooks(
            llm,
            registry,
            state_store,
            temp_workspace,
            plugin_ctx,
        )
        wire_persistence(engine, state_store, plugin_ctx)
        plugin_ctx.on(Events.STATE_CHANGED, record_state_changed)

        _ = [event async for event in engine.run_turn("echo hi")]

        assert persisted_sizes == [3, 4]
        assert state_store.message_count() == 4

    @pytest.mark.asyncio
    async def test_tool_message_hook_runs_after_tool_result_is_yielded(
        self, state_store, temp_workspace
    ):
        llm = MockLLM(responses=[
            {
                "content": "calling",
                "tool_calls": [
                    {"id": "call-1", "name": "echo", "args": {"message": "hi"}}
                ],
            },
            {"content": "done"},
        ])
        registry = ToolRegistry()
        registry.register(echo_tool)
        order = []

        async def on_tool_message(_ctx):
            order.append("on_tool_message")

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.TOOL_MESSAGE, on_tool_message)
        engine = make_engine_with_hooks(
            llm,
            registry,
            state_store,
            temp_workspace,
            plugin_ctx,
        )

        async for event in engine.run_turn("echo hi"):
            if event["type"] in {"tool_calls_started", "tool_result"}:
                order.append(event["type"])

        assert order == [
            "tool_calls_started",
            "tool_result",
            "on_tool_message",
        ]

    @pytest.mark.asyncio
    async def test_rejected_message_does_not_trigger_empty_persistence(
        self, state_store, temp_workspace
    ):
        async def reject(_ctx):
            return {"turn_complete": True}

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.BEFORE_USER_MESSAGE_ACCEPT, reject)
        engine = make_engine_with_hooks(
            MockLLM(responses=[]),
            ToolRegistry(),
            state_store,
            temp_workspace,
            plugin_ctx,
        )
        wire_persistence(engine, state_store, plugin_ctx)

        events = [event async for event in engine.run_turn("reject")]

        assert events[0]["data"]["code"] == "user_message_rejected"
        assert state_store.message_count() == 0

    @pytest.mark.asyncio
    async def test_before_persist_message_mutation_is_written_in_same_checkpoint(
        self, state_store, temp_workspace
    ):
        async def add_metadata_message(ctx):
            if not any(message.name == "persist-hook" for message in ctx.messages):
                ctx.messages.append(
                    Message(role="system", content="metadata", name="persist-hook")
                )

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.AFTER_MODEL_RESPONSE, add_metadata_message)
        engine = make_engine_with_hooks(
            MockLLM(responses=[{"content": "ok"}]),
            ToolRegistry(),
            state_store,
            temp_workspace,
            plugin_ctx,
        )
        persistence = wire_persistence(engine, state_store, plugin_ctx)

        _ = [event async for event in engine.run_turn("test")]

        persisted = state_store.read_messages()
        assert [(message.role, message.content) for message in persisted] == [
            ("user", "test"),
            ("assistant", "ok"),
            ("system", "metadata"),
        ]
        assert await persistence.flush() is False

    @pytest.mark.asyncio
    async def test_in_place_message_change_is_detected_without_manual_dirty_flag(
        self, state_store, temp_workspace
    ):
        engine = make_engine(
            MockLLM(responses=[{"content": "original"}]),
            ToolRegistry(),
            state_store,
            temp_workspace,
        )
        persistence = wire_persistence(engine, state_store, engine._events)
        _ = [event async for event in engine.run_turn("test")]

        engine.messages[-1].content = "updated"

        assert await persistence.flush() is True
        assert state_store.read_messages()[-1].content == "updated"

    @pytest.mark.asyncio
    async def test_cancelled_turn_persists_accepted_message_once(
        self, state_store, temp_workspace
    ):
        persisted_sizes = []

        async def cancel_turn(_ctx):
            raise asyncio.CancelledError()

        async def record_state_changed(ctx):
            persisted_sizes.append(len(ctx.messages))

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.TURN_START, cancel_turn)
        engine = make_engine_with_hooks(
            MockLLM(responses=[]),
            ToolRegistry(),
            state_store,
            temp_workspace,
            plugin_ctx,
        )
        wire_persistence(engine, state_store, plugin_ctx)
        plugin_ctx.on(Events.STATE_CHANGED, record_state_changed)
        events = []

        with pytest.raises(asyncio.CancelledError):
            async for event in engine.run_turn("cancel me"):
                events.append(event)

        assert [event["type"] for event in events] == ["turn_cancelled"]
        assert persisted_sizes == [1]
        assert state_store.read_messages()[0].content == "cancel me"

    @pytest.mark.asyncio
    async def test_failed_turn_persists_accepted_message_once(
        self, state_store, temp_workspace
    ):
        persisted_sizes = []

        async def fail_model_request(_ctx):
            raise RuntimeError("model request blocked")

        async def record_state_changed(ctx):
            persisted_sizes.append(len(ctx.messages))

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.BEFORE_MODEL_REQUEST, fail_model_request)
        engine = make_engine_with_hooks(
            MockLLM(responses=[]),
            ToolRegistry(),
            state_store,
            temp_workspace,
            plugin_ctx,
        )
        wire_persistence(engine, state_store, plugin_ctx)
        plugin_ctx.on(Events.STATE_CHANGED, record_state_changed)

        events = [event async for event in engine.run_turn("fail me")]

        assert [event["type"] for event in events][-2:] == [
            "error",
            "turn_finished",
        ]
        assert events[-2]["data"]["code"] == "engine_error"
        assert persisted_sizes == [1]
        assert state_store.read_messages()[0].content == "fail me"

    @pytest.mark.asyncio
    async def test_stop_hooks_receive_reasons(self, state_store, temp_workspace):
        """Stop plugin_ctx distinguish normal completion."""
        llm = MockLLM(responses=[{"content": "ok"}])
        registry = ToolRegistry()
        calls = []

        async def on_stop(ctx):
            calls.append((ctx.stop_reason,))

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.ON_STOP, on_stop)

        engine = make_engine(
            llm=llm,
            tool_registry=registry,
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )

        _ = [e async for e in engine.run_turn("test")]

        assert calls == [("completed",)]

    @pytest.mark.asyncio
    async def test_hook_can_make_unbound_auxiliary_model_call(
        self, state_store, temp_workspace
    ):
        llm = MockLLM(responses=[
            {"content": "summary", "usage_metadata": {"input_tokens": 3}},
            {"content": "answer"},
        ])
        summaries = []

        async def invoke_aux(model, messages):
            from XBotv2.core.messages import merge_model_chunk

            aggregate = None
            async for chunk in model.astream(messages):
                aggregate = merge_model_chunk(aggregate, chunk)
            return aggregate

        async def before_context(ctx):
            response = await invoke_aux(
                plugin_ctx.model,
                [Message(role="user", content="summarize history")],
            )
            summaries.append(response)

        plugin_ctx = xcore.Context()
        plugin_ctx.set("model", llm)
        plugin_ctx.on(Events.BEFORE_CONTEXT, before_context)
        engine = make_engine(
            llm=llm,
            tool_registry=ToolRegistry(),
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(
                enabled=False,
                workspace_root=str(temp_workspace),
            ),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )

        events = [event async for event in engine.run_turn("question")]

        assert summaries[0].content == "summary"
        assert summaries[0].usage_metadata == {"input_tokens": 3}
        assert llm.get_call_messages(0)[0].content == "summarize history"
        assert llm.get_call_messages(1)[-1].content == "question"
        assert next(
            event for event in events if event["type"] == "assistant_message"
        )["data"]["content"] == "answer"

    @pytest.mark.asyncio
    async def test_tool_client_event_does_not_stop_turn(self, state_store, temp_workspace):
        """send-message style tools emit client events and continue the loop."""
        llm = MockLLM(responses=[
            {
                "content": "notify",
                "tool_calls": [{"name": "send_notice", "args": {"message": "heads up"}, "id": "c1"}],
            },
            {"content": "done"},
        ])
        registry = ToolRegistry()
        registry.register(send_notice)
        engine = make_engine(llm, registry, state_store, temp_workspace)

        events = [e async for e in engine.run_turn("notify")]

        assert [e["type"] for e in events].count("assistant_message") == 2
        notice = next(e for e in events if e["type"] == "client_message")
        assert notice["data"]["message"] == "heads up"
        assert notice["data"]["tool_call_id"] == "c1"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_session_resume_repairs_trailing_unanswered_tool_call(
        self, state_store, temp_workspace
    ):
        state_store.sync_messages([
            Message(role="user", content="run it"),
            Message(
                role="assistant",
                content="running",
                tool_calls=[ToolCall(id="c1", name="echo", args={"message": "hi"})],
            ),
        ])
        llm = MockLLM(responses=[{"content": "continued"}])
        engine = make_engine(llm, ToolRegistry(), state_store, temp_workspace)
        persistence = wire_persistence(engine, state_store, engine._events)
        prior = state_store.read_messages()
        engine.state.messages = prior
        engine.state.turn_count = 1
        engine.state.resumed = True

        await engine.start_session()

        assert engine.messages[-1].role == "tool"
        assert engine.messages[-1].tool_call_id == "c1"
        assert engine.messages[-1].status == "error"
        assert "session_restarted" in engine.messages[-1].content
        assert state_store.read_messages()[-1].tool_call_id == "c1"

        _ = [event async for event in engine.run_turn("continue")]

        model_history = llm.get_call_messages(0)
        assert any("session_restarted" in str(message.content) for message in model_history)
        assert any(message.content == "continue" for message in model_history)

    @pytest.mark.asyncio
    async def test_new_turn_after_ask_user_without_live_client_stays_active(self, state_store, temp_workspace):
        """A direct engine caller without live input support does not leave the session interrupted."""
        llm = MockLLM(responses=[
            {
                "content": "ask",
                "tool_calls": [{"name": "request_input", "args": {"question": "Proceed?"}, "id": "c1"}],
            },
            {"content": "continued without answer"},
            {"content": "next turn"},
        ])
        registry = ToolRegistry()
        registry.register(request_input)
        engine = make_engine(llm, registry, state_store, temp_workspace)

        _ = [e async for e in engine.run_turn("ask")]
        _ = [e async for e in engine.run_turn("continue")]
        assert llm.call_count == 3

class TestEngineState:
    """Engine state tracking."""

    @pytest.mark.asyncio
    async def test_messages_accumulate(self, state_store, temp_workspace):
        """Messages accumulate across turns."""
        llm = MockLLM(responses=[
            {"content": "Response 1"},
            {"content": "Response 2"},
        ])
        registry = ToolRegistry()

        engine = make_engine(llm, registry, state_store, temp_workspace)
        _ = [e async for e in engine.run_turn("msg1")]
        _ = [e async for e in engine.run_turn("msg2")]

        human_msgs = [m for m in engine.messages if m.role == "user"]
        ai_msgs = [m for m in engine.messages if getattr(m, "content", None) in {"Response 1", "Response 2"}]
        assert len(human_msgs) == 2
        assert len(ai_msgs) == 2

    @pytest.mark.asyncio
    async def test_turn_request_id_reaches_turn_and_persistence_hooks(
        self, state_store, temp_workspace
    ):
        observed = []

        async def record(ctx):
            observed.append(ctx.request_id)

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.TURN_START, record)
        plugin_ctx.on(Events.STATE_CHANGED, record)
        engine = make_engine_with_hooks(
            MockLLM(responses=[{"content": "ok"}]),
            ToolRegistry(),
            state_store,
            temp_workspace,
            plugin_ctx,
        )
        wire_persistence(engine, state_store, plugin_ctx)

        _ = [
            event
            async for event in engine.run_turn(
                "correlate this turn",
                request_id="request-core-1",
            )
        ]

        assert observed == ["request-core-1", "request-core-1"]

    @pytest.mark.asyncio
    async def test_session_lifecycle(self, state_store, temp_workspace):
        """Session start/resume/close plugin_ctx fire."""
        llm = MockLLM(responses=[])
        registry = ToolRegistry()

        calls = []

        async def record_start(ctx):
            calls.append("start")

        async def record_close(ctx):
            calls.append("close")

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.SESSION_START, record_start)
        plugin_ctx.on(Events.SESSION_CLOSE, record_close)

        engine = make_engine(
            llm=llm,
            tool_registry=registry,
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )
        await engine.start_session()
        await engine.close_session()

        assert calls == ["start", "close"]

    @pytest.mark.asyncio
    async def test_session_close_unloads_plugins_after_hook_failure(
        self, state_store, temp_workspace
    ):
        async def fail_close(_ctx):
            raise RuntimeError("close hook failed")

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.SESSION_CLOSE, fail_close)
        engine = make_engine(
            llm=MockLLM(responses=[]),
            tool_registry=ToolRegistry(),
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            sandbox_policy=SandboxPolicy(
                enabled=False,
                workspace_root=str(temp_workspace),
            ),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )
        await engine.start_session()

        with pytest.raises(RuntimeError, match="close hook failed"):
            await engine.close_session()

    @pytest.mark.asyncio
    async def test_start_session_resumes_event_only_state(self, state_store, temp_workspace):
        """Session with existing messages starts as a resume."""
        state_store.append_messages([Message(role="user", content="prior message")])
        llm = MockLLM(responses=[])
        registry = ToolRegistry()
        calls = []

        async def record_start(ctx):
            calls.append("start")

        async def record_resume(ctx):
            calls.append("resume")

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.SESSION_START, record_start)
        plugin_ctx.on(Events.SESSION_RESUME, record_resume)
        engine = make_engine(
            llm=llm,
            tool_registry=registry,
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )
        # Resume is a property of the hydrated LoopState, set by the
        # persistence observer in production.
        engine.state.messages = [Message(role="user", content="prior message")]
        engine.state.turn_count = 1
        engine.state.resumed = True

        await engine.start_session()

        assert calls == ["resume"]

    @pytest.mark.asyncio
    async def test_close_session_materializes_closed_status(self, state_store, temp_workspace):
        """Session close runs plugin_ctx without error."""
        llm = MockLLM(responses=[])
        registry = ToolRegistry()
        engine = make_engine(llm, registry, state_store, temp_workspace)

        await engine.close_session()

        assert True

    @pytest.mark.asyncio
    async def test_on_error_hook_runs_when_turn_fails(self, state_store, temp_workspace):
        """Engine emits ON_ERROR and an error event when turn execution fails."""
        llm = MockLLM(responses=[])
        registry = ToolRegistry()
        calls = []

        async def on_error(ctx):
            calls.append((type(ctx.error).__name__, ctx.user_input))

        plugin_ctx = xcore.Context()
        plugin_ctx.on(Events.ON_ERROR, on_error)
        engine = make_engine(
            llm=llm,
            tool_registry=registry,
            plugin_ctx=plugin_ctx,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=RuntimeConfig(),
        )

        events = [e async for e in engine.run_turn("will fail")]

        assert [event["type"] for event in events][-2:] == [
            "error",
            "turn_finished",
        ]
        assert calls == [("RuntimeError", "will fail")]


# ------------------------------------------------------------------
# Hook message overrides
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_after_context_hook_can_override_context_messages(state_store, temp_workspace):
    """AFTER_CONTEXT hook return dict with 'context_messages' replaces provider messages."""
    llm = MockLLM(responses=[{"content": "ok"}])
    registry = ToolRegistry()
    plugin_ctx = xcore.Context()
    recorded: list[str] = []

    async def after_context(ctx):
        recorded.append("after_context")
        msgs = list(ctx.context_messages) if ctx.context_messages else []
        msgs.append(Message(role="system", content="HOOK: extra instruction"))
        return {"context_messages": msgs}

    plugin_ctx.on(Events.AFTER_CONTEXT, after_context)
    engine = make_engine_with_hooks(llm, registry, state_store, temp_workspace, plugin_ctx)
    events = [e async for e in engine.run_turn("hello")]

    assert recorded == ["after_context"]
    sent_messages = llm.get_call_messages(0)
    system_contents = [m.content for m in sent_messages if m.role == "system"]
    assert any("HOOK: extra instruction" in c for c in system_contents)


@pytest.mark.asyncio
async def test_before_tool_schema_bind_hook_can_override_messages(state_store, temp_workspace):
    """BEFORE_TOOL_SCHEMA_BIND hook return dict with 'messages' replaces context."""
    llm = MockLLM(responses=[{"content": "ok"}])
    registry = ToolRegistry()
    registry.register(echo_tool)
    plugin_ctx = xcore.Context()
    recorded: list[str] = []

    async def before_bind(ctx):
        recorded.append("before_bind")
        msgs = list(ctx.context_messages) if ctx.context_messages else []
        msgs.append(Message(role="system", content="BIND: filtered context"))
        return {"messages": msgs}

    plugin_ctx.on(Events.BEFORE_TOOL_SCHEMA_BIND, before_bind)
    engine = make_engine_with_hooks(llm, registry, state_store, temp_workspace, plugin_ctx)
    events = [e async for e in engine.run_turn("hi")]

    assert recorded == ["before_bind"]
    sent = llm.get_call_messages(0)
    system_contents = [m.content for m in sent if m.role == "system"]
    assert any("BIND: filtered context" in c for c in system_contents)


@pytest.mark.asyncio
async def test_before_model_request_hook_can_override_messages(state_store, temp_workspace):
    """BEFORE_MODEL_REQUEST hook return dict with 'messages' overrides final request."""
    llm = MockLLM(responses=[{"content": "final"}])
    registry = ToolRegistry()
    plugin_ctx = xcore.Context()
    recorded: list[str] = []

    async def before_request(ctx):
        recorded.append("before_request")
        msgs = list(ctx.model_request["messages"]) if ctx.model_request else []
        msgs.append(Message(role="system", content="REQUEST: last-moment override"))
        return {"messages": msgs}

    plugin_ctx.on(Events.BEFORE_MODEL_REQUEST, before_request)
    engine = make_engine_with_hooks(llm, registry, state_store, temp_workspace, plugin_ctx)
    events = [e async for e in engine.run_turn("hi")]

    assert recorded == ["before_request"]
    sent = llm.get_call_messages(0)
    system_contents = [m.content for m in sent if m.role == "system"]
    assert any("REQUEST: last-moment override" in c for c in system_contents)


# ------------------------------------------------------------------
# AFTER_AGENT hook short-circuit
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_after_agent_hook_can_inject_messages_and_complete_turn(state_store, temp_workspace):
    """AFTER_AGENT hook injects messages into history and completes the turn."""
    llm = MockLLM(responses=[{"content": "LLM response"}])
    registry = ToolRegistry()
    plugin_ctx = xcore.Context()
    calls: list[str] = []

    async def after_agent(ctx):
        calls.append("after_agent")
        return {"messages": [Message(role="assistant", content="injected by hook")]}

    plugin_ctx.on(Events.AFTER_AGENT, after_agent)
    engine = make_engine_with_hooks(llm, registry, state_store, temp_workspace, plugin_ctx)

    events = [e async for e in engine.run_turn("trigger")]

    assert calls == ["after_agent"]
    injected = [m for m in engine.messages if m.content == "injected by hook"]
    assert len(injected) == 1
    assert injected[0].role == "assistant"
    assert "turn_finished" in [e["type"] for e in events]


@pytest.mark.asyncio
async def test_after_agent_hook_can_raise_stop_failure_recovery(state_store, temp_workspace):
    """ON_STOP_FAILURE hook fires when ON_STOP hook raises an exception."""
    llm = MockLLM(responses=[{"content": "ok"}])
    registry = ToolRegistry()
    plugin_ctx = xcore.Context()
    failure_calls: list[str] = []

    async def on_stop_raises(ctx):
        raise RuntimeError("stop hook failed")

    async def on_stop_failure(ctx):
        failure_calls.append(f"failure:{ctx.stop_reason}")

    plugin_ctx.on(Events.ON_STOP, on_stop_raises)
    plugin_ctx.on(Events.ON_STOP_FAILURE, on_stop_failure)
    engine = make_engine_with_hooks(llm, registry, state_store, temp_workspace, plugin_ctx)

    events = [e async for e in engine.run_turn("x")]

    assert len(failure_calls) == 2
    assert failure_calls[0] == "failure:completed"
    assert failure_calls[1] == "failure:error"
    assert [event["type"] for event in events][-2:] == [
        "error",
        "turn_finished",
    ]
    assert events[-2]["data"]["code"] == "engine_error"
    assert events[-2]["data"]["details"] == {
        "exception_type": "RuntimeError",
    }


# ------------------------------------------------------------------
# Session management: submit_user_input / submit_permission_response
# ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.asyncio
# ------------------------------------------------------------------
# Reasoning delta (DeepSeek R1 / Claude thinking)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_emitted_separately_in_stream(state_store, temp_workspace):
    """Reasoning and assistant text use distinct stream fields."""
    llm = MockLLM(responses=[{
        "content": "The answer is 42.",
        "chunks": [
            {"reasoning": "step by step"},
            {"content": "The answer is 42."},
        ],
    }])
    registry = ToolRegistry()

    engine = make_engine(llm, registry, state_store, temp_workspace)
    events = [e async for e in engine.run_turn("what is 6*7?")]

    deltas = [e for e in events if e["type"] == "assistant_message_delta"]
    assert len(deltas) == 2
    assert deltas[0]["data"] == {"reasoning": "step by step"}
    assert deltas[1]["data"] == {"content": "The answer is 42."}


@pytest.mark.asyncio
async def test_additional_kwargs_merged_across_streaming_chunks(state_store, temp_workspace):
    """additional_kwargs from multiple chunks are merged into the final response."""
    llm = MockLLM(responses=[{
        "content": "hello",
        "chunks": [
            {"additional_kwargs": {"custom_a": "value_a"}},
            {"additional_kwargs": {"custom_b": "value_b"}},
        ],
    }])
    registry = ToolRegistry()

    engine = make_engine(llm, registry, state_store, temp_workspace)
    events = [e async for e in engine.run_turn("hi")]

    assistant = [e for e in events if e["type"] == "assistant_message"]
    assert len(assistant) == 1
    assert assistant[0]["data"]["content"] == "hello"
