"""Tests for the core Engine — ReAct loop with NO plugins."""

import pytest
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.tools import tool as langchain_tool

from xbotv2.core.engine import Engine
from xbotv2.core.context import ContextBuilder, ContextComponent
from xbotv2.hooks.manager import HookManager
from xbotv2.hooks.types import HookStage
from xbotv2.llm.mock import MockLLM
from xbotv2.tools.registry import ToolRegistry
from xbotv2.tools.permissions import PermissionSystem
from xbotv2.tools.sandbox import SandboxPolicy


@langchain_tool
def echo(message: str) -> str:
    """Echo a message back."""
    return f"Echo: {message}"


@langchain_tool
def shout(message: str) -> str:
    """Uppercase a message."""
    return message.upper()


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


class TestEngineBasics:
    """Basic ReAct loop behavior."""

    @pytest.mark.asyncio
    async def test_simple_text_response(self, state_store, temp_workspace):
        """Engine returns a text response when no tool calls are made."""
        llm = MockLLM(responses=[{"content": "Hello! How can I help?"}])
        registry = ToolRegistry()
        registry.register(echo, sandbox_mode="host")

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
        registry.register(echo, sandbox_mode="host")

        engine = make_engine(llm, registry, state_store, temp_workspace)
        events = [e async for e in engine.run_turn("echo hello")]

        types = [e["type"] for e in events]
        assert "tool_calls_started" in types
        assert "tool_result" in types
        # Should have two assistant events (pre-tool and post-tool)
        assistant_events = [e for e in events if e["type"] == "assistant_message"]
        assert len(assistant_events) == 2

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
        registry.register(echo, sandbox_mode="host")

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
        registry.register(echo, sandbox_mode="host")

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
                "messages": [AIMessage(content="Hijacked!")]
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
        registry.register(echo, sandbox_mode="host")
        calls = []

        async def after_context_build(ctx):
            calls.append(("context", len(ctx.context_messages)))

        async def after_tool_schema_bind(ctx):
            calls.append(("tools", [tool.name for tool in ctx.model_request["tools"]]))

        async def before_model_request(ctx):
            calls.append(("request", len(ctx.model_request["messages"])))

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
    async def test_user_message_accept_short_circuit_emits_error(
        self, state_store, temp_workspace
    ):
        """Input-intake hooks cannot silently leave protocol clients waiting."""
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

        assert events == [
            {
                "type": "error",
                "data": {
                    "code": "user_message_rejected",
                    "message": "User message was rejected before entering history.",
                },
            }
        ]
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
        assert "system_prefix" in calls[1][1]
        assert "history" in calls[1][1]
        assert any("from hook" in content for content in calls[2][1])
        assert any("Hook Component" in content for content in calls[2][1])

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
                object.__setattr__(self, "bound_names", [tool.name for tool in tools])
                return self

        llm = RecordingLLM()
        registry = ToolRegistry()
        registry.register(echo, sandbox_mode="host")

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
                self.bound_history.append([tool.name for tool in tools])
                return self

        llm = RecordingLLM()
        registry = ToolRegistry()
        registry.register(echo, sandbox_mode="host")
        registry.register(shout, sandbox_mode="host")

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
            calls.append(("model", type(ctx.error).__name__))

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

        assert events[-1]["type"] == "error"
        assert calls == [("model", "RuntimeError"), ("engine", "RuntimeError")]

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
        registry.register(echo, sandbox_mode="host")
        calls = []

        async def parsed(ctx):
            calls.append(("parsed", [call["name"] for call in ctx.tool_calls]))

        async def before_call(ctx):
            calls.append(("before", ctx.tool_call["name"]))

        async def after_call(ctx):
            calls.append(("after", ctx.tool_call["name"], ctx.tool_result.status))

        async def denied(ctx):
            calls.append(("denied", ctx.tool_call["name"], type(ctx.error).__name__))

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

        assert calls[0] == ("before", 2, 0)
        assert calls[1] == ("after", 2, 2)


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

        human_msgs = [m for m in engine.messages if isinstance(m, HumanMessage)]
        ai_msgs = [m for m in engine.messages if isinstance(m, AIMessage)]
        assert len(human_msgs) == 2
        assert len(ai_msgs) == 2

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
        await engine.close_session()

        assert "on_session_start" in calls
        assert "on_session_close" in calls

    @pytest.mark.asyncio
    async def test_close_session_materializes_closed_status(self, state_store, temp_workspace):
        """Session close persists the closed materialized state."""
        llm = MockLLM(responses=[])
        registry = ToolRegistry()
        engine = make_engine(llm, registry, state_store, temp_workspace)

        await engine.close_session()

        assert state_store.read_state()["status"] == "closed"

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

        assert events[-1]["type"] == "error"
        assert calls == [(HookStage.ON_ERROR, "RuntimeError", "will fail")]
        assert state_store.read_state()["status"] == "error"
