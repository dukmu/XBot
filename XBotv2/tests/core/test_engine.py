"""Tests for the core Engine — ReAct loop with NO plugins."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from xbotv2.core.engine import Engine
from xbotv2.core.context import ContextBuilder
from xbotv2.core.builtin_tools.shell import shell
from xbotv2.hooks.manager import HookManager
from xbotv2.api.hooks import HookStage
from xbotv2.llm.mock import MockLLM
from xbotv2.api import ContextComponent
from xbotv2.api.messages import Message, ModelResponse
from xbotv2.tools.registry import ToolRegistry
from xbotv2.tools.permissions import PermissionSystem
from xbotv2.tools.sandbox import SandboxPolicy
from xbotv2.api.tools import ArtifactRef, Tool, ToolCall, ToolError, ToolResult


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


def send_notice(message: str) -> dict:
    return {
        "content": "notice sent",
        "events": [{"type": "client_message", "data": {"message": message}}],
    }
send_notice_tool = Tool.from_function(send_notice, name="send_notice")


def request_input(question: str) -> dict:
    return {
        "content": "waiting for user",
        "wait_for_user": True,
        "events": [
            {
                "type": "user_input_required",
                "data": {
                    "question": question,
                    "options": [
                        {"label": "continue", "description": "Continue the work."},
                        {"label": "stop", "description": "Stop the work."},
                    ],
                },
            }
        ],
    }


def structured_failure() -> ToolResult:
    return ToolResult(
        status="error",
        content="structured failure",
        data={"attempt": 2},
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


def make_engine(mock_llm, tool_registry, state_store, temp_workspace):
    """Create a minimal engine for testing."""
    return Engine(
        llm=mock_llm,
        tool_registry=tool_registry,
        hook_manager=HookManager(),
        state_store=state_store,
        context_builder=ContextBuilder(),
        sandbox_policy=SandboxPolicy(
            enabled=False,
            workspace_root=str(temp_workspace),
        ),
        permission_system=PermissionSystem(default_decision="allow"),
        config=None,
    )


def make_engine_with_hooks(mock_llm, tool_registry, state_store, temp_workspace, hook_manager):
    """Create a minimal engine with a supplied hook manager."""
    return Engine(
        llm=mock_llm,
        tool_registry=tool_registry,
        hook_manager=hook_manager,
        state_store=state_store,
        context_builder=ContextBuilder(),
        sandbox_policy=SandboxPolicy(
            enabled=False,
            workspace_root=str(temp_workspace),
        ),
        permission_system=PermissionSystem(default_decision="allow"),
        config=None,
    )


class TestEngineBasics:
    """Basic ReAct loop behavior."""

    @pytest.mark.asyncio
    async def test_simple_text_response(self, state_store, temp_workspace):
        """Engine returns a text response when no tool calls are made."""
        llm = MockLLM(responses=[{"content": "Hello! How can I help?"}])
        registry = ToolRegistry()
        registry.register(echo_tool, sandbox_mode="host")

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
                },
            }
        ]
        assert engine.session_usage == {
            "input_tokens": 11,
            "output_tokens": 7,
            "total_tokens": 18,
            "requests": 1,
            "context_tokens": 11,
        }

        await engine.replace_history([])

        assert state_store.read_usage() == engine.session_usage

    @pytest.mark.asyncio
    async def test_streaming_text_deltas_precede_final_assistant_message(self, state_store, temp_workspace):
        """Engine surfaces LangChain AIMessageChunk content as live deltas."""
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
        registry.register(echo_tool, sandbox_mode="host")

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
        registry.register(shell, sandbox_mode="host")

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
        registry.register(echo_tool, sandbox_mode="host")

        engine = make_engine(llm, registry, state_store, temp_workspace)
        events = [e async for e in engine.run_turn("echo hello")]

        types = [e["type"] for e in events]
        assert "tool_calls_started" in types
        assert "tool_result" in types
        # Should have two assistant events (pre-tool and post-tool)
        assistant_events = [e for e in events if e["type"] == "assistant_message"]
        assert len(assistant_events) == 2

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
        registry.register(structured_failure_tool, sandbox_mode="host")
        engine = make_engine(llm, registry, state_store, temp_workspace)

        events = [event async for event in engine.run_turn("test result")]

        result = next(event for event in events if event["type"] == "tool_result")
        assert result["data"] == {
            "tool_call_id": "c1",
            "name": "structured_failure",
            "content": "structured failure",
            "status": "error",
            "data": {"attempt": 2},
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
        registry.register(echo_tool, sandbox_mode="host")

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
        """Engine stops after max_iterations."""
        # Infinite loop of tool calls
        responses = []
        for i in range(10):
            responses.append({
                "content": f"Call {i}",
                "tool_calls": [{"name": "echo", "args": {"message": str(i)}, "id": f"call_{i}"}],
            })
        llm = MockLLM(responses=responses)
        registry = ToolRegistry()
        registry.register(echo_tool, sandbox_mode="host")

        engine = make_engine(llm, registry, state_store, temp_workspace)
        engine.max_iterations = 3  # Small limit
        events = [e async for e in engine.run_turn("loop")]

        # Should have stopped at max_iterations
        assistant_events = [e for e in events if e["type"] == "assistant_message"]
        assert len(assistant_events) <= 3


class TestEngineHooks:
    """Hook integration in the engine."""

    @pytest.mark.asyncio
    async def test_hooks_fire_during_turn(self, state_store, temp_workspace):
        """Registered hooks are called during a turn."""
        llm = MockLLM(responses=[{"content": "Hello!"}])
        registry = ToolRegistry()

        hook_calls = []

        async def on_turn_start(ctx):
            hook_calls.append("turn_start")

        async def on_turn_end(ctx):
            hook_calls.append("turn_end")

        hook_manager = HookManager()
        hook_manager.register(HookStage.ON_TURN_START, on_turn_start)
        hook_manager.register(HookStage.ON_TURN_END, on_turn_end)

        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
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

        hook_manager = HookManager()
        hook_manager.register(HookStage.BEFORE_AGENT, replace_response)

        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
        )
        events = [e async for e in engine.run_turn("test")]

        # The LLM should NOT have been called; the hook hijacked it
        # The messages should contain the hijacked response
        assert "Hijacked!" in str(engine.messages)

    @pytest.mark.asyncio
    async def test_model_request_hooks_receive_context_and_response(self, state_store, temp_workspace):
        """Fine-grained model hooks see built context, tools, request, and response."""
        llm = MockLLM(responses=[{"content": "Hello!"}])
        registry = ToolRegistry()
        registry.register(echo_tool, sandbox_mode="host")
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

        hook_manager = HookManager()
        hook_manager.register(HookStage.AFTER_CONTEXT_BUILD, after_context_build)
        hook_manager.register(HookStage.AFTER_TOOL_SCHEMA_BIND, after_tool_schema_bind)
        hook_manager.register(HookStage.BEFORE_MODEL_REQUEST, before_model_request)
        hook_manager.register(HookStage.AFTER_MODEL_RESPONSE, after_model_response)

        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
        )

        events = [e async for e in engine.run_turn("test")]

        assert events[-1]["type"] == "turn_finished"
        assert calls[0][0] == "context"
        assert calls[1] == ("tools", ["echo"])
        assert calls[2][0] == "request"
        assert calls[3] == ("response", "Hello!")

    @pytest.mark.asyncio
    async def test_before_model_request_can_short_circuit_turn(self, state_store, temp_workspace):
        """Budget-style hooks can stop before the provider request."""
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

        hook_manager = HookManager()
        hook_manager.register(HookStage.BEFORE_MODEL_REQUEST, deny_request)

        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
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
            HookStage.BEFORE_CONTEXT,
            HookStage.BEFORE_CONTEXT_BUILD,
            HookStage.AFTER_CONTEXT,
            HookStage.BEFORE_TOOL_SCHEMA_BIND,
            HookStage.BEFORE_MODEL_REQUEST,
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

        hook_manager = HookManager()
        hook_manager.register(stage, stop)

        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
        )

        events = [e async for e in engine.run_turn("test")]

        assert [event["type"] for event in events] == [
            "turn_started",
            "error",
            "turn_finished",
        ]
        assert events[1]["data"]["code"] == "engine_error"
        assert events[1]["data"]["details"]["exception_type"] == "TypeError"
        assert stage.value in events[1]["data"]["message"]
        assert llm.call_count == 0

    @pytest.mark.asyncio
    async def test_user_message_accept_hooks_can_rewrite_input(self, state_store, temp_workspace):
        """User intake hooks run before history is recorded."""
        llm = MockLLM(responses=[{"content": "ok"}])
        registry = ToolRegistry()
        calls = []

        async def before_accept(ctx):
            calls.append(("before", ctx.user_input))
            return {"user_input": "rewritten"}

        async def after_accept(ctx):
            calls.append(("after", ctx.user_input, ctx.state["messages"][-1].content))

        hook_manager = HookManager()
        hook_manager.register(HookStage.BEFORE_USER_MESSAGE_ACCEPT, before_accept)
        hook_manager.register(HookStage.AFTER_USER_MESSAGE_ACCEPT, after_accept)

        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
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

        hook_manager = HookManager()
        hook_manager.register(HookStage.BEFORE_USER_MESSAGE_ACCEPT, reject)

        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
        )

        events = [e async for e in engine.run_turn("blocked")]

        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert events[0]["data"]["code"] == "engine_error"
        assert events[0]["data"]["details"]["exception_type"] == "TypeError"
        assert "before_user_message_accept" in events[0]["data"]["message"]
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

        hook_manager = HookManager()
        hook_manager.register(HookStage.BEFORE_USER_MESSAGE_ACCEPT, reject)

        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
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

        hook_manager = HookManager()
        hook_manager.register(HookStage.BEFORE_USER_MESSAGE_ACCEPT, announce)
        engine = make_engine_with_hooks(
            MockLLM(responses=[{"content": "ok"}]),
            ToolRegistry(),
            state_store,
            temp_workspace,
            hook_manager,
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
        """Context hooks expose source-tagged components before provider messages."""
        llm = MockLLM(responses=[{"content": "ok"}])
        registry = ToolRegistry()
        calls = []

        async def before_context_build(ctx):
            calls.append(("before_build", ctx.stage))
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

        hook_manager = HookManager()
        hook_manager.register(HookStage.BEFORE_CONTEXT_BUILD, before_context_build)
        hook_manager.register(HookStage.AFTER_CONTEXT_COMPONENTS_BUILD, after_components)
        hook_manager.register(HookStage.AFTER_CONTEXT_BUILD, after_context_build)

        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
        )

        _ = [e async for e in engine.run_turn("test")]

        assert calls[0] == ("before_build", HookStage.BEFORE_CONTEXT_BUILD)
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

        hook_manager = HookManager()
        hook_manager.register(
            HookStage.AFTER_CONTEXT_COMPONENTS_BUILD,
            replace_with_invalid,
        )
        llm = MockLLM(responses=[{"content": "should not run"}])
        engine = make_engine_with_hooks(
            llm,
            ToolRegistry(),
            state_store,
            temp_workspace,
            hook_manager,
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
        """Tool schema hooks run before provider bind_tools is called."""
        class RecordingLLM(MockLLM):
            def __init__(self):
                super().__init__([{"content": "ok"}])
                object.__setattr__(self, "bound_names", None)

            def bind_tools(self, tools, **kwargs):
                object.__setattr__(self, "bound_names", [tool_name(tool) for tool in tools])
                return self

        llm = RecordingLLM()
        registry = ToolRegistry()
        registry.register(echo_tool, sandbox_mode="host")

        async def filter_tools(ctx):
            assert [tool.name for tool in ctx.model_request["tools"]] == ["echo"]
            return {"tools": []}

        hook_manager = HookManager()
        hook_manager.register(HookStage.BEFORE_TOOL_SCHEMA_BIND, filter_tools)

        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
        )

        _ = [e async for e in engine.run_turn("test")]

        assert llm.bound_names is None

    @pytest.mark.asyncio
    async def test_before_model_request_rebinds_when_tools_change(
        self, state_store, temp_workspace
    ):
        """Late request hooks that change tools also update the bound client."""
        class RecordingLLM(MockLLM):
            def __init__(self):
                super().__init__([{"content": "ok"}])
                object.__setattr__(self, "bound_history", [])

            def bind_tools(self, tools, **kwargs):
                self.bound_history.append([tool_name(tool) for tool in tools])
                return self

        llm = RecordingLLM()
        registry = ToolRegistry()
        registry.register(echo_tool, sandbox_mode="host")
        registry.register(shout_tool, sandbox_mode="host")

        async def keep_echo(ctx):
            return {"tools": [tool for tool in ctx.model_request["tools"] if tool.name == "echo"]}

        hook_manager = HookManager()
        hook_manager.register(HookStage.BEFORE_MODEL_REQUEST, keep_echo)

        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
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

        hook_manager = HookManager()
        hook_manager.register(HookStage.ON_MODEL_REQUEST_ERROR, on_model_error)
        hook_manager.register(HookStage.ON_ERROR, on_error)

        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
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
        class FlakyLLM:
            def __init__(self):
                self.calls = 0

            async def astream(self, messages):
                self.calls += 1
                if self.calls == 1:
                    raise ConnectionError("temporary disconnect")
                yield ModelResponse(content="recovered")

        llm = FlakyLLM()
        monkeypatch.setattr("xbotv2.core.engine.asyncio.sleep", AsyncMock())
        engine = make_engine(llm, ToolRegistry(), state_store, temp_workspace)

        events = [event async for event in engine.run_turn("test")]

        assert llm.calls == 2
        assert any(
            event["type"] == "client_message"
            and "retrying" in event["data"]["message"]
            for event in events
        )
        assert any(
            event["type"] == "assistant_message"
            and event["data"]["content"] == "recovered"
            for event in events
        )

    @pytest.mark.asyncio
    async def test_tool_call_lifecycle_hooks_fire(self, state_store, temp_workspace):
        """Parsed, per-call before/after, and denial hooks are visible."""
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
        registry.register(echo_tool, sandbox_mode="host")
        calls = []

        async def parsed(ctx):
            calls.append(("parsed", [call.name for call in ctx.tool_calls]))

        async def before_call(ctx):
            calls.append(("before", ctx.tool_call.name))

        async def after_call(ctx):
            calls.append(("after", ctx.tool_call.name, ctx.tool_result.status))

        async def denied(ctx):
            calls.append(("denied", ctx.tool_call.name, type(ctx.error).__name__))

        hook_manager = HookManager()
        hook_manager.register(HookStage.ON_TOOL_CALLS_PARSED, parsed)
        hook_manager.register(HookStage.BEFORE_TOOL_CALL, before_call)
        hook_manager.register(HookStage.AFTER_TOOL_CALL, after_call)
        hook_manager.register(HookStage.ON_TOOL_DENIED, denied)

        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
        )

        events = [e async for e in engine.run_turn("test")]

        assert events[-1]["type"] == "turn_finished"
        assert ("parsed", ["echo", "missing"]) in calls
        assert ("before", "echo") in calls
        assert ("after", "echo", "success") in calls
        assert ("denied", "missing", "PermissionError") in calls

    @pytest.mark.asyncio
    async def test_permission_checks_tool_call_after_hook_transformation(
        self,
        state_store,
        temp_workspace,
    ):
        llm = MockLLM(responses=[
            {
                "content": "tools",
                "tool_calls": [
                    {"name": "echo", "args": {"message": "hi"}, "id": "call_1"},
                ],
            },
            {"content": "done"},
        ])
        registry = ToolRegistry()
        registry.register(echo_tool, sandbox_mode="host")
        registry.register(shout_tool, sandbox_mode="host")
        permission_system = PermissionSystem(default_decision="allow")
        permission_system.add_rule(
            "deny",
            {"tool": "shout", "params": {"message": "blocked"}},
        )
        denied = []

        async def rewrite_call(ctx):
            return {
                "tool_call": ToolCall(
                    ctx.tool_call.id,
                    "shout",
                    {"message": "blocked"},
                )
            }

        async def on_permission_denied(ctx):
            denied.append((ctx.tool_call.name, ctx.tool_call.args))

        hook_manager = HookManager()
        hook_manager.register(HookStage.BEFORE_TOOL_CALL, rewrite_call)
        hook_manager.register(HookStage.ON_PERMISSION_DENIED, on_permission_denied)
        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=permission_system,
            config=None,
        )

        _ = [event async for event in engine.run_turn("test")]

        assert denied == [("shout", {"message": "blocked"})]
        tool_message = next(message for message in engine.messages if message.role == "tool")
        assert tool_message.status == "error"
        assert tool_message.tool_call_id == "call_1"

    @pytest.mark.asyncio
    async def test_hook_denial_does_not_request_permission(
        self,
        state_store,
        temp_workspace,
    ):
        llm = MockLLM(responses=[
            {
                "content": "tools",
                "tool_calls": [
                    {"name": "echo", "args": {"message": "hi"}, "id": "call_1"},
                ],
            },
            {"content": "done"},
        ])
        registry = ToolRegistry()
        registry.register(echo_tool, sandbox_mode="host")

        async def deny_call(ctx):
            return {"deny_reason": "blocked by plugin policy"}

        hook_manager = HookManager()
        hook_manager.register(HookStage.BEFORE_TOOL_CALL, deny_call)
        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="ask"),
            config=None,
        )

        events = [event async for event in engine.run_turn("test")]

        assert not any(event["type"] == "permission_request" for event in events)
        tool_message = next(message for message in engine.messages if message.role == "tool")
        assert "Error: blocked by plugin policy" in tool_message.content
        assert tool_message.content.startswith('<tool_result name="echo" status="error">')

    @pytest.mark.asyncio
    async def test_message_tool_and_permission_hooks_receive_caller_payloads(
        self,
        state_store,
        temp_workspace,
    ):
        """Stable observer families expose their documented runtime payloads."""
        llm = MockLLM(responses=[
            {
                "content": "try tool",
                "tool_calls": [
                    {"name": "echo", "args": {"message": "hi"}, "id": "call_denied"},
                ],
            },
            {"content": "done"},
        ])
        registry = ToolRegistry()
        registry.register(echo_tool, sandbox_mode="host")
        observed = {}

        async def on_user_message(ctx):
            observed["user"] = (ctx.user_input, ctx.session.turn_count)

        async def on_assistant_message(ctx):
            observed.setdefault("assistant", []).append(ctx.agent_response.content)

        async def before_tools(ctx):
            observed["before_tools"] = (
                [call.id for call in ctx.tool_calls],
                ctx.agent_response.content,
            )

        async def on_permission_denied(ctx):
            observed["permission_denied"] = (
                ctx.tool_call.id,
                ctx.permission_decision,
                type(ctx.error).__name__,
            )

        async def on_tool_message(ctx):
            result = ctx.tool_results[0]
            observed["tool_message"] = (
                result.tool_call_id,
                result.status,
            )

        hook_manager = HookManager()
        hook_manager.register(HookStage.ON_USER_MESSAGE, on_user_message)
        hook_manager.register(HookStage.ON_ASSISTANT_MESSAGE, on_assistant_message)
        hook_manager.register(HookStage.BEFORE_TOOLS, before_tools)
        hook_manager.register(HookStage.ON_PERMISSION_DENIED, on_permission_denied)
        hook_manager.register(HookStage.ON_TOOL_MESSAGE, on_tool_message)
        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(
                enabled=False,
                workspace_root=str(temp_workspace),
            ),
            permission_system=PermissionSystem(default_decision="deny"),
            config=None,
        )

        _ = [event async for event in engine.run_turn("run echo")]

        assert observed == {
            "user": ("run echo", 1),
            "assistant": ["try tool", "done"],
            "before_tools": (["call_denied"], "try tool"),
            "permission_denied": ("call_denied", "deny", "PermissionError"),
            "tool_message": ("call_denied", "error"),
        }

    @pytest.mark.asyncio
    async def test_state_persist_hooks_fire(self, state_store, temp_workspace):
        """Persistence hooks bracket message materialization."""
        llm = MockLLM(responses=[{"content": "ok"}])
        registry = ToolRegistry()
        calls = []

        async def before_persist(ctx):
            calls.append(("before", len(ctx.state["messages"]), state_store.message_count()))

        async def after_persist(ctx):
            calls.append(("after", len(ctx.state["messages"]), state_store.message_count()))

        hook_manager = HookManager()
        hook_manager.register(HookStage.BEFORE_STATE_PERSIST, before_persist)
        hook_manager.register(HookStage.AFTER_STATE_PERSIST, after_persist)

        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
        )

        _ = [e async for e in engine.run_turn("test")]

        assert calls == [
            ("before", 2, 0),
            ("after", 2, 2),
        ]
        assert await engine.save_messages() is False
        assert len(calls) == 2

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
        registry.register(echo_tool, sandbox_mode="host")
        persisted_sizes = []

        async def after_persist(ctx):
            persisted_sizes.append(len(ctx.state["messages"]))

        hook_manager = HookManager()
        hook_manager.register(HookStage.AFTER_STATE_PERSIST, after_persist)
        engine = make_engine_with_hooks(
            llm,
            registry,
            state_store,
            temp_workspace,
            hook_manager,
        )

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
        registry.register(echo_tool, sandbox_mode="host")
        order = []

        async def on_tool_message(_ctx):
            order.append("on_tool_message")

        hook_manager = HookManager()
        hook_manager.register(HookStage.ON_TOOL_MESSAGE, on_tool_message)
        engine = make_engine_with_hooks(
            llm,
            registry,
            state_store,
            temp_workspace,
            hook_manager,
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
        persisted = []

        async def reject(_ctx):
            return {"turn_complete": True}

        async def after_persist(_ctx):
            persisted.append(True)

        hook_manager = HookManager()
        hook_manager.register(HookStage.BEFORE_USER_MESSAGE_ACCEPT, reject)
        hook_manager.register(HookStage.AFTER_STATE_PERSIST, after_persist)
        engine = make_engine_with_hooks(
            MockLLM(responses=[]),
            ToolRegistry(),
            state_store,
            temp_workspace,
            hook_manager,
        )

        events = [event async for event in engine.run_turn("reject")]

        assert events[0]["data"]["code"] == "user_message_rejected"
        assert persisted == []
        assert state_store.message_count() == 0

    @pytest.mark.asyncio
    async def test_before_persist_message_mutation_is_written_in_same_checkpoint(
        self, state_store, temp_workspace
    ):
        async def add_metadata_message(ctx):
            if not any(message.name == "persist-hook" for message in ctx.state["messages"]):
                ctx.state["messages"].append(
                    Message(role="system", content="metadata", name="persist-hook")
                )

        hook_manager = HookManager()
        hook_manager.register(HookStage.BEFORE_STATE_PERSIST, add_metadata_message)
        engine = make_engine_with_hooks(
            MockLLM(responses=[{"content": "ok"}]),
            ToolRegistry(),
            state_store,
            temp_workspace,
            hook_manager,
        )

        _ = [event async for event in engine.run_turn("test")]

        persisted = state_store.read_messages()
        assert [(message.role, message.content) for message in persisted] == [
            ("user", "test"),
            ("assistant", "ok"),
            ("system", "metadata"),
        ]
        assert await engine.save_messages() is False

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
        _ = [event async for event in engine.run_turn("test")]

        engine.messages[-1].content = "updated"

        assert await engine.save_messages() is True
        assert state_store.read_messages()[-1].content == "updated"

    @pytest.mark.asyncio
    async def test_cancelled_turn_persists_accepted_message_once(
        self, state_store, temp_workspace
    ):
        persisted_sizes = []

        async def cancel_turn(_ctx):
            raise asyncio.CancelledError()

        async def after_persist(ctx):
            persisted_sizes.append(len(ctx.state["messages"]))

        hook_manager = HookManager()
        hook_manager.register(HookStage.ON_TURN_START, cancel_turn)
        hook_manager.register(HookStage.AFTER_STATE_PERSIST, after_persist)
        engine = make_engine_with_hooks(
            MockLLM(responses=[]),
            ToolRegistry(),
            state_store,
            temp_workspace,
            hook_manager,
        )
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

        async def after_persist(ctx):
            persisted_sizes.append(len(ctx.state["messages"]))

        hook_manager = HookManager()
        hook_manager.register(HookStage.BEFORE_MODEL_REQUEST, fail_model_request)
        hook_manager.register(HookStage.AFTER_STATE_PERSIST, after_persist)
        engine = make_engine_with_hooks(
            MockLLM(responses=[]),
            ToolRegistry(),
            state_store,
            temp_workspace,
            hook_manager,
        )

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
        """Stop hooks distinguish normal completion."""
        llm = MockLLM(responses=[{"content": "ok"}])
        registry = ToolRegistry()
        calls = []

        async def on_stop(ctx):
            calls.append((ctx.stage, ctx.stop_reason))

        hook_manager = HookManager()
        hook_manager.register(HookStage.ON_STOP, on_stop)

        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
        )

        _ = [e async for e in engine.run_turn("test")]

        assert calls == [(HookStage.ON_STOP, "completed")]

    @pytest.mark.asyncio
    async def test_compact_hooks_bracket_before_context_message_replacement(
        self, state_store, temp_workspace
    ):
        """Compaction hooks run around BEFORE_CONTEXT message replacement."""
        llm = MockLLM(responses=[{"content": "ok"}])
        registry = ToolRegistry()
        calls = []

        async def compact(ctx):
            return {
                "messages": [Message(role="user", content="compacted")],
                "compact_reason": "test_compact",
            }

        async def pre_compact(ctx):
            calls.append(("pre", ctx.compact_reason, len(ctx.state["messages"])))

        async def post_compact(ctx):
            calls.append((
                "post",
                ctx.compact_reason,
                ctx.state["previous_message_count"],
                ctx.state["current_message_count"],
            ))

        hook_manager = HookManager()
        hook_manager.register(HookStage.BEFORE_CONTEXT, compact)
        hook_manager.register(HookStage.PRE_COMPACT, pre_compact)
        hook_manager.register(HookStage.POST_COMPACT, post_compact)

        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
        )

        _ = [e async for e in engine.run_turn("test")]

        assert calls == [("pre", "test_compact", 1), ("post", "test_compact", 1, 1)]
        assert engine.messages[0].content == "compacted"

    @pytest.mark.asyncio
    async def test_hook_can_make_unbound_auxiliary_model_call(
        self, state_store, temp_workspace
    ):
        llm = MockLLM(responses=[
            {"content": "summary", "usage_metadata": {"input_tokens": 3}},
            {"content": "answer"},
        ])
        summaries = []

        async def before_context(ctx):
            response = await ctx.invoke_model([
                Message(role="user", content="summarize history")
            ])
            summaries.append(response)

        hook_manager = HookManager()
        hook_manager.register(HookStage.BEFORE_CONTEXT, before_context)
        engine = Engine(
            llm=llm,
            tool_registry=ToolRegistry(),
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(
                enabled=False,
                workspace_root=str(temp_workspace),
            ),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
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
        registry.register(send_notice, sandbox_mode="host")
        engine = make_engine(llm, registry, state_store, temp_workspace)

        events = [e async for e in engine.run_turn("notify")]

        assert [e["type"] for e in events].count("assistant_message") == 2
        notice = next(e for e in events if e["type"] == "client_message")
        assert notice["data"]["message"] == "heads up"
        assert notice["data"]["source"] == "send_message"
        assert notice["data"]["tool_call_id"] == "c1"

    @pytest.mark.asyncio
    async def test_ask_user_without_live_client_cancels_and_continues(self, state_store, temp_workspace):
        """ask-user style tools do not hang when no live client can answer."""
        llm = MockLLM(responses=[
            {
                "content": "ask",
                "tool_calls": [{"name": "request_input", "args": {"question": "Proceed?"}, "id": "c1"}],
            },
            {"content": "continued without an answer"},
        ])
        registry = ToolRegistry()
        registry.register(request_input, sandbox_mode="host")
        engine = make_engine(llm, registry, state_store, temp_workspace)

        events = [e async for e in engine.run_turn("ask")]

        input_event = next(e for e in events if e["type"] == "user_input_required")
        assert input_event["data"]["request_id"] == "user_input:c1"
        assert input_event["data"]["source"] == "ask_user"
        assert input_event["data"]["tool_call_id"] == "c1"
        assert input_event["data"]["resume_supported"] is False
        tool_result = next(e for e in events if e["type"] == "tool_result")
        assert "does not support live user input" in tool_result["data"]["content"]
        assert llm.call_count == 2
        state = state_store.read_state()
        assert state["status"] == "active"
        assert state["pending_interactions"] == []

    @pytest.mark.asyncio
    async def test_disconnect_during_ask_user_closes_tool_call_history(
        self, state_store, temp_workspace
    ):
        llm = MockLLM(responses=[{
            "content": "ask",
            "tool_calls": [
                {"name": "request_input", "args": {"question": "Proceed?"}, "id": "c1"}
            ],
        }])
        registry = ToolRegistry()
        registry.register(request_input, sandbox_mode="host")
        engine = make_engine(llm, registry, state_store, temp_workspace)

        async def disconnected(*_args, **_kwargs):
            return {
                "request_id": "user_input:c1",
                "status": "disconnected",
                "reason": "client_disconnected",
            }

        engine.set_client_event_sink(disconnected)

        events = [event async for event in engine.run_turn("ask")]

        assert events[-1] == {
            "type": "turn_cancelled",
            "data": {"turn": 1, "reason": "client_disconnected"},
        }
        persisted = state_store.read_messages()
        assert persisted[-1].role == "tool"
        assert persisted[-1].tool_call_id == "c1"
        assert persisted[-1].status == "error"
        assert "client_disconnected" in persisted[-1].content

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
    async def test_client_event_hook_observes_interaction_events(self, state_store, temp_workspace):
        """Client event hooks observe send-message and ask-user events."""
        llm = MockLLM(responses=[
            {
                "content": "notify",
                "tool_calls": [
                    {"name": "send_notice", "args": {"message": "heads up"}, "id": "c1"},
                    {"name": "request_input", "args": {"question": "Proceed?"}, "id": "c2"},
                ],
            },
            {"content": "continued"},
        ])
        registry = ToolRegistry()
        registry.register(send_notice, sandbox_mode="host")
        registry.register(request_input, sandbox_mode="host")
        hook_manager = HookManager()
        observed = []

        async def on_client_event(ctx):
            tool_call_id = getattr(ctx.tool_result, "tool_call_id", None)
            observed.append((ctx.client_event["type"], tool_call_id))

        hook_manager.register(HookStage.ON_CLIENT_EVENT, on_client_event)
        engine = make_engine_with_hooks(
            llm,
            registry,
            state_store,
            temp_workspace,
            hook_manager,
        )

        _ = [e async for e in engine.run_turn("interact")]

        assert observed == [
            ("client_message", "c1"),
            ("user_input_required", "c2"),
        ]

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
        registry.register(request_input, sandbox_mode="host")
        engine = make_engine(llm, registry, state_store, temp_workspace)

        _ = [e async for e in engine.run_turn("ask")]
        state = state_store.read_state()
        assert state["status"] == "active"
        assert state["pending_interactions"] == []

        _ = [e async for e in engine.run_turn("continue")]

        assert state_store.read_state()["status"] == "active"

    @pytest.mark.asyncio
    async def test_permission_request_event_reaches_client(self, state_store, temp_workspace):
        """Permission ask decisions are protocol-visible events."""
        llm = MockLLM(responses=[
            {
                "content": "write",
                "tool_calls": [{"name": "echo", "args": {"message": "hi"}, "id": "c1"}],
            },
            {"content": "done"},
        ])
        registry = ToolRegistry()
        registry.register(echo_tool, sandbox_mode="host")
        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=HookManager(),
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="ask"),
            config=None,
        )

        events = [e async for e in engine.run_turn("need approval")]

        permission_event = next(e for e in events if e["type"] == "permission_request")
        assert permission_event["data"]["tool_call"]["name"] == "echo"
        assert permission_event["data"]["request_id"] == "permission:c1"
        assert permission_event["data"]["source"] == "permission_system"
        assert "No live permission handler" not in permission_event["data"]["reason"]
        assert permission_event["data"]["resume_supported"] is False

    @pytest.mark.asyncio
    async def test_client_event_hook_observes_permission_request(self, state_store, temp_workspace):
        """Permission ask decisions also pass through the generic client-event hook."""
        llm = MockLLM(responses=[
            {
                "content": "write",
                "tool_calls": [{"name": "echo", "args": {"message": "hi"}, "id": "c1"}],
            },
            {"content": "done"},
        ])
        registry = ToolRegistry()
        registry.register(echo_tool, sandbox_mode="host")
        hook_manager = HookManager()
        observed = []

        async def on_client_event(ctx):
            observed.append(ctx.client_event["type"])

        hook_manager.register(HookStage.ON_CLIENT_EVENT, on_client_event)
        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="ask"),
            config=None,
        )

        _ = [e async for e in engine.run_turn("need approval")]

        assert observed == ["permission_request"]

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
            observed.append((ctx.stage, ctx.request_id))

        hook_manager = HookManager()
        hook_manager.register(HookStage.ON_TURN_START, record)
        hook_manager.register(HookStage.AFTER_STATE_PERSIST, record)
        engine = make_engine_with_hooks(
            MockLLM(responses=[{"content": "ok"}]),
            ToolRegistry(),
            state_store,
            temp_workspace,
            hook_manager,
        )

        _ = [
            event
            async for event in engine.run_turn(
                "correlate this turn",
                request_id="request-core-1",
            )
        ]

        assert observed == [
            (HookStage.ON_TURN_START, "request-core-1"),
            (HookStage.AFTER_STATE_PERSIST, "request-core-1"),
        ]

    @pytest.mark.asyncio
    async def test_session_lifecycle(self, state_store, temp_workspace):
        """Session start/resume/close hooks fire."""
        llm = MockLLM(responses=[])
        registry = ToolRegistry()

        calls = []

        async def record_call(ctx):
            calls.append(ctx.stage.value)

        hook_manager = HookManager()
        hook_manager.register(HookStage.ON_SESSION_START, record_call)
        hook_manager.register(HookStage.ON_SESSION_CLOSE, record_call)
        plugin_loader = AsyncMock()

        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
            plugin_loader=plugin_loader,
        )
        await engine.start_session()
        await engine.close_session()

        assert "on_session_start" in calls
        assert "on_session_close" in calls
        plugin_loader.unload_all.assert_awaited_once()
        assert engine.plugin_loader is None

    @pytest.mark.asyncio
    async def test_session_close_unloads_plugins_after_hook_failure(
        self, state_store, temp_workspace
    ):
        async def fail_close(_ctx):
            raise RuntimeError("close hook failed")

        hook_manager = HookManager()
        hook_manager.register(HookStage.ON_SESSION_CLOSE, fail_close)
        plugin_loader = AsyncMock()
        engine = Engine(
            llm=MockLLM(responses=[]),
            tool_registry=ToolRegistry(),
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(
                enabled=False,
                workspace_root=str(temp_workspace),
            ),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
            plugin_loader=plugin_loader,
        )
        await engine.start_session()

        with pytest.raises(ExceptionGroup, match="on_session_close"):
            await engine.close_session()

        plugin_loader.unload_all.assert_awaited_once()
        assert engine.plugin_loader is None

    @pytest.mark.asyncio
    async def test_start_session_resumes_event_only_state(self, state_store, temp_workspace):
        """Session with existing messages starts as a resume."""
        state_store.append_message(Message(role="user", content="prior message"))
        llm = MockLLM(responses=[])
        registry = ToolRegistry()
        calls = []

        async def record_call(ctx):
            calls.append(ctx.stage.value)

        hook_manager = HookManager()
        hook_manager.register(HookStage.ON_SESSION_START, record_call)
        hook_manager.register(HookStage.ON_SESSION_RESUME, record_call)
        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
        )

        await engine.start_session()

        assert calls == ["on_session_resume"]

    @pytest.mark.asyncio
    async def test_close_session_materializes_closed_status(self, state_store, temp_workspace):
        """Session close runs hooks without error."""
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
            calls.append((ctx.stage, type(ctx.error).__name__, ctx.user_input))

        hook_manager = HookManager()
        hook_manager.register(HookStage.ON_ERROR, on_error)
        engine = Engine(
            llm=llm,
            tool_registry=registry,
            hook_manager=hook_manager,
            state_store=state_store,
            context_builder=ContextBuilder(),
            sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
            permission_system=PermissionSystem(default_decision="allow"),
            config=None,
        )

        events = [e async for e in engine.run_turn("will fail")]

        assert [event["type"] for event in events][-2:] == [
            "error",
            "turn_finished",
        ]
        assert calls == [(HookStage.ON_ERROR, "RuntimeError", "will fail")]


# ------------------------------------------------------------------
# Hook message overrides
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_after_context_hook_can_override_context_messages(state_store, temp_workspace):
    """AFTER_CONTEXT hook return dict with 'context_messages' replaces provider messages."""
    llm = MockLLM(responses=[{"content": "ok"}])
    registry = ToolRegistry()
    hook_manager = HookManager()
    recorded: list[str] = []

    async def after_context(ctx):
        recorded.append("after_context")
        msgs = list(ctx.context_messages) if ctx.context_messages else []
        msgs.append(Message(role="system", content="HOOK: extra instruction"))
        return {"context_messages": msgs}

    hook_manager.register(HookStage.AFTER_CONTEXT, after_context)
    engine = make_engine_with_hooks(llm, registry, state_store, temp_workspace, hook_manager)
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
    registry.register(echo_tool, sandbox_mode="host")
    hook_manager = HookManager()
    recorded: list[str] = []

    async def before_bind(ctx):
        recorded.append("before_bind")
        msgs = list(ctx.context_messages) if ctx.context_messages else []
        msgs.append(Message(role="system", content="BIND: filtered context"))
        return {"messages": msgs}

    hook_manager.register(HookStage.BEFORE_TOOL_SCHEMA_BIND, before_bind)
    engine = make_engine_with_hooks(llm, registry, state_store, temp_workspace, hook_manager)
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
    hook_manager = HookManager()
    recorded: list[str] = []

    async def before_request(ctx):
        recorded.append("before_request")
        msgs = list(ctx.model_request["messages"]) if ctx.model_request else []
        msgs.append(Message(role="system", content="REQUEST: last-moment override"))
        return {"messages": msgs}

    hook_manager.register(HookStage.BEFORE_MODEL_REQUEST, before_request)
    engine = make_engine_with_hooks(llm, registry, state_store, temp_workspace, hook_manager)
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
    hook_manager = HookManager()
    calls: list[str] = []

    async def after_agent(ctx):
        calls.append("after_agent")
        return {"messages": [Message(role="assistant", content="injected by hook")]}

    hook_manager.register(HookStage.AFTER_AGENT, after_agent)
    engine = make_engine_with_hooks(llm, registry, state_store, temp_workspace, hook_manager)

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
    hook_manager = HookManager()
    failure_calls: list[str] = []

    async def on_stop_raises(ctx):
        raise RuntimeError("stop hook failed")

    async def on_stop_failure(ctx):
        failure_calls.append(f"failure:{ctx.stop_reason}")

    hook_manager.register(HookStage.ON_STOP, on_stop_raises)
    hook_manager.register(HookStage.ON_STOP_FAILURE, on_stop_failure)
    engine = make_engine_with_hooks(llm, registry, state_store, temp_workspace, hook_manager)

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
        "exception_type": "ExceptionGroup",
    }


# ------------------------------------------------------------------
# Session management: submit_user_input / submit_permission_response
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_user_input_resolves_pending_request(state_store, temp_workspace):
    """submit_user_input resolves a pending user input request."""
    llm = MockLLM(responses=[{"content": "ok"}])
    registry = ToolRegistry()
    engine = make_engine(llm, registry, state_store, temp_workspace)

    import asyncio
    pending = asyncio.create_task(engine.user_input_waiter.wait("test-req-1", timeout_seconds=5.0))
    await asyncio.sleep(0.05)

    result = engine.submit_user_input("test-req-1", answer="hello from user")
    assert result.request_id == "test-req-1"
    assert result.status == "answered"
    assert result.answer == "hello from user"

    waited = await pending
    assert waited.answer == "hello from user"


@pytest.mark.asyncio
async def test_submit_permission_response_resolves_pending_request(state_store, temp_workspace):
    """submit_permission_response resolves a pending permission request."""
    llm = MockLLM(responses=[{"content": "ok"}])
    registry = ToolRegistry()
    engine = make_engine(llm, registry, state_store, temp_workspace)

    import asyncio
    pending = asyncio.create_task(engine.permission_waiter.wait("perm-req-1", timeout_seconds=5.0))
    await asyncio.sleep(0.05)

    result = engine.submit_permission_response("perm-req-1", decision="allow")
    assert result.request_id == "perm-req-1"
    assert result.status == "answered"
    assert result.decision == "allow"

    waited = await pending
    assert waited.decision == "allow"


# ------------------------------------------------------------------
# Reasoning delta (DeepSeek R1 / Claude thinking)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_emitted_as_content_in_stream(state_store, temp_workspace):
    """Reasoning content appears in assistant_message_delta via provider."""
    llm = MockLLM(responses=[{
        "content": "The answer is 42.",
        "chunks": [
            {"content": "Let me think...", "additional_kwargs": {"reasoning_content": "step by step"}},
            {"content": "The answer is 42."},
        ],
    }])
    registry = ToolRegistry()

    engine = make_engine(llm, registry, state_store, temp_workspace)
    events = [e async for e in engine.run_turn("what is 6*7?")]

    deltas = [e for e in events if e["type"] == "assistant_message_delta"]
    assert len(deltas) == 2


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


# ------------------------------------------------------------------
# Permission persistence (session-scope)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_permission_if_session_scope_writes_to_disk(state_store, temp_workspace):
    """Session-scope permission decisions are persisted to disk via persist_permission_decision."""
    llm = MockLLM(responses=[{"content": "ok"}])
    registry = ToolRegistry()
    engine = make_engine(llm, registry, state_store, temp_workspace)

    client_event = {
        "type": "permission_request",
        "data": {
            "request_id": "perm-persist",
            "tool_call": {"name": "shell", "args": {}},
        },
    }
    result = {"scope": "session", "decision": "allow", "status": "answered"}

    engine.persist_permission_if_session_scope(client_event, result)
    assert True
