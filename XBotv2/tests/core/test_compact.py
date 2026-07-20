"""Behavior tests for the built-in conversation compaction plugin."""

import asyncio
import json
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from builtin_plugins.compact.plugin import (
    CompactPlugin,
    _compact_prefix_end,
    _history_chars,
)
from xbotv2.api import (
    HookContext,
    HookStage,
    Message,
    ModelResponse,
    PluginManifest,
    ToolCall,
)
from xbotv2.core.context import ContextBuilder
from xbotv2.core.engine import Engine
from xbotv2.hooks.manager import HookManager
from xbotv2.llm.mock import MockLLM
from xbotv2.tools.permissions import PermissionSystem
from xbotv2.tools.registry import ToolRegistry
from xbotv2.tools.sandbox import SandboxPolicy


def make_plugin() -> CompactPlugin:
    return CompactPlugin(
        PluginManifest(name="compact", version="1"),
        store=None,
    )


class SetupContext:
    def __init__(self) -> None:
        self.hooks = {}
        self.tool = None
        self.options = None
        self.commands = {}

    def register_hook(self, stage, callback):
        self.hooks[stage] = callback

    def register_tool(self, tool, options=None):
        self.tool = tool
        self.options = options
        return "plugin:compact:compact"

    def register_command(self, command):
        self.commands[command.name] = command
        return command.name


def history(turns: int, *, content: str = "message") -> list[Message]:
    messages = []
    for index in range(turns):
        messages.extend([
            Message(role="user", content=f"user {index} {content}"),
            Message(role="assistant", content=f"assistant {index} {content}"),
        ])
    return messages


def test_compact_prefix_preserves_recent_complete_turns():
    messages = history(3)
    messages[3].tool_calls = [ToolCall("call-1", "shell", {"command": "pwd"})]
    messages.insert(
        4,
        Message(role="tool", content="/tmp", tool_call_id="call-1"),
    )

    split = _compact_prefix_end(messages, keep_recent_turns=2)

    assert [message.role for message in messages[split:]] == [
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_manual_tool_requests_compaction_below_threshold():
    plugin = make_plugin()
    await plugin.on_load({"automatic": False, "keep_recent_turns": 1})
    setup = SetupContext()
    plugin.setup(setup)
    tool_result = await setup.tool.ainvoke({})

    async def invoke_model(messages):
        request = ET.fromstring(messages[-1].content)
        assert request.tag == "summary_request"
        assert request.text.strip() == "Produce the conversation summary now."
        return ModelResponse(content="Important earlier context")

    original = history(3)
    result = await setup.hooks[HookStage.BEFORE_CONTEXT](
        HookContext(
            stage=HookStage.BEFORE_CONTEXT,
            state={"messages": original},
            session=SimpleNamespace(turn_count=3),
            invoke_model=invoke_model,
        )
    )

    assert tool_result.data == {"requested": True}
    assert result["compact_reason"] == "manual"
    assert result["messages"][0].role == "system"
    assert "Important earlier context" in result["messages"][0].content
    assert result["messages"][1:] == original[-2:]


@pytest.mark.asyncio
async def test_human_command_compacts_and_persists_immediately(
    caplog,
    state_store,
    temp_workspace,
):
    caplog.set_level("INFO", logger="xbotv2.compact")
    plugin = make_plugin()
    await plugin.on_load({"automatic": False, "keep_recent_turns": 1})
    setup = SetupContext()
    plugin.setup(setup)
    hooks = HookManager()
    hooks.register(HookStage.BEFORE_CONTEXT, setup.hooks[HookStage.BEFORE_CONTEXT])
    original = history(3)
    state_store.sync_messages(original)
    llm = MockLLM(responses=[{
        "content": "Earlier requirements.",
        "usage_metadata": {
            "input_tokens": 30,
            "output_tokens": 4,
            "total_tokens": 34,
        },
    }])
    engine = Engine(
        llm=llm,
        tool_registry=ToolRegistry(),
        hook_manager=hooks,
        state_store=state_store,
        context_builder=ContextBuilder(),
        sandbox_policy=SandboxPolicy(
            enabled=False,
            workspace_root=str(temp_workspace),
        ),
        permission_system=PermissionSystem(default_decision="allow"),
        config=None,
    )
    await engine.start_session()
    runtime_events = []
    engine.runtime_event_sink = runtime_events.append
    command_ctx = SimpleNamespace(turn_lock=asyncio.Lock(), engine=engine)

    result = await setup.commands["compact"].handler(command_ctx, "")

    records = [
        json.loads(line)
        for line in state_store.messages_path.read_text(encoding="utf-8").splitlines()
    ]
    checkpoint = next(
        record for record in records
        if record.get("record_type") == "history_checkpoint"
    )
    assert checkpoint["reason"] == "compact:manual"
    assert any(record.get("content") == "user 0 message" for record in records)

    assert result.status == "ok"
    assert result.data["requested"] is True
    assert result.data["compacted"] is True
    history_chars_before = _history_chars(original)
    history_chars_after = _history_chars(engine.messages)
    assert result.data["metrics"]["history_chars_before"] == history_chars_before
    assert result.data["metrics"]["history_chars_after"] == history_chars_after
    assert result.data["metrics"]["summary_chars"] == 21
    assert result.data["metrics"]["model_usage"] == {
        "input_tokens": 30,
        "output_tokens": 4,
        "total_tokens": 34,
        "context_tokens": 30,
    }
    assert [event["type"] for event in runtime_events] == [
        "compaction_started",
        "compaction_completed",
    ]
    assert runtime_events[-1]["data"]["usage"]["total_tokens"] == 34
    assert (
        f"{history_chars_before} to {history_chars_after} characters"
        in result.message
    )
    assert "30 input and 4 output tokens" in result.message
    assert (
        f"history_chars_before={history_chars_before} "
        f"history_chars_after={history_chars_after}"
    ) in caplog.text
    assert "input_tokens=30 output_tokens=4 total_tokens=34" in caplog.text
    assert llm.call_count == 1
    assert engine.messages[0].role == "system"
    assert "Earlier requirements." in engine.messages[0].content
    assert state_store.read_messages() == engine.messages
    assert command_ctx.turn_lock.locked() is False


@pytest.mark.asyncio
async def test_human_command_runs_when_active_turn_becomes_idle():
    plugin = make_plugin()
    await plugin.on_load({"automatic": False})
    setup = SetupContext()
    plugin.setup(setup)
    turn_lock = asyncio.Lock()
    await turn_lock.acquire()
    calls = 0

    class EngineStub:
        async def run_context_maintenance(self):
            nonlocal calls
            calls += 1
            return True

    command_ctx = SimpleNamespace(turn_lock=turn_lock, engine=EngineStub())

    command_task = asyncio.create_task(
        setup.commands["compact"].handler(command_ctx, "")
    )
    await asyncio.sleep(0)

    assert command_task.done() is False
    assert calls == 0

    turn_lock.release()
    result = await command_task

    assert result.status == "ok"
    assert result.data == {"requested": True, "compacted": True}
    assert calls == 1
    assert turn_lock.locked() is False


@pytest.mark.asyncio
async def test_compaction_does_not_append_duplicate_human_directives():
    plugin = make_plugin()
    await plugin.on_load({"automatic": False, "keep_recent_turns": 1})
    plugin._manual_requested = True
    original = history(3)
    original[2].content = "Do not ask me again; decide the safest option."

    async def invoke_model(_messages):
        return ModelResponse(
            content="## Conversation Summary\n\nOlder context only."
        )

    result = await plugin._on_before_context(HookContext(
        stage=HookStage.BEFORE_CONTEXT,
        state={"messages": original},
        session=SimpleNamespace(turn_count=3),
        invoke_model=invoke_model,
    ))

    summary = result["messages"][0].content
    root = ET.fromstring(summary)
    assert root.tag == "conversation_summary"
    assert "## Recent Human Directives (verbatim)" not in summary
    assert summary.count("Older context only.") == 1


@pytest.mark.asyncio
async def test_automatic_compaction_can_run_again_after_history_grows():
    plugin = make_plugin()
    await plugin.on_load({
        "trigger_chars": 1000,
        "keep_recent_turns": 1,
        "summary_max_chars": 500,
    })
    calls = 0

    async def invoke_model(messages):
        nonlocal calls
        calls += 1
        return ModelResponse(content="summary")

    original = history(3, content="x" * 300)
    ctx = HookContext(
        stage=HookStage.BEFORE_CONTEXT,
        state={"messages": original},
        session=SimpleNamespace(turn_count=4),
        invoke_model=invoke_model,
    )

    first = await plugin._on_before_context(ctx)
    ctx.state["messages"] = first["messages"]
    second = await plugin._on_before_context(ctx)

    assert first["compact_reason"] == "automatic"
    assert second is None
    assert calls == 1

    ctx.state["messages"] = [
        *first["messages"],
        *history(3, content="y" * 300),
    ]
    third = await plugin._on_before_context(ctx)

    assert third["compact_reason"] == "automatic"
    assert calls == 2


@pytest.mark.asyncio
async def test_automatic_compaction_uses_latest_provider_context_usage():
    plugin = make_plugin()
    await plugin.on_load({
        "trigger_chars": 10_000,
        "output_reservation": 100,
        "trigger_ratio": 0.8,
        "keep_recent_turns": 1,
    })
    original = history(3, content="x" * 300)
    original[-1].usage_metadata = {"input_tokens": 700}
    calls = 0

    async def invoke_model(_messages):
        nonlocal calls
        calls += 1
        return ModelResponse(content="summary")

    ctx = HookContext(
        stage=HookStage.BEFORE_CONTEXT,
        state={"messages": original},
        config=SimpleNamespace(max_context_tokens=1000),
        session=SimpleNamespace(turn_count=4),
        invoke_model=invoke_model,
    )

    assert await plugin._on_before_context(ctx) is None
    assert calls == 0

    original[-1].usage_metadata = {"input_tokens": 720}
    result = await plugin._on_before_context(ctx)

    assert result["compact_reason"] == "automatic"
    assert calls == 1


@pytest.mark.asyncio
async def test_character_threshold_is_only_used_without_provider_usage():
    plugin = make_plugin()
    await plugin.on_load({"trigger_chars": 1000, "keep_recent_turns": 1})
    original = history(3, content="x" * 300)

    async def invoke_model(_messages):
        return ModelResponse(content="summary")

    ctx = HookContext(
        stage=HookStage.BEFORE_CONTEXT,
        state={"messages": original},
        session=SimpleNamespace(turn_count=4),
        invoke_model=invoke_model,
    )

    result = await plugin._on_before_context(ctx)

    assert result["compact_reason"] == "automatic"


@pytest.mark.asyncio
async def test_character_threshold_overrides_underreported_provider_usage():
    plugin = make_plugin()
    await plugin.on_load({"trigger_chars": 1000, "keep_recent_turns": 1})
    original = history(3, content="x" * 300)
    original[-1].usage_metadata = {"input_tokens": 10}

    async def invoke_model(_messages):
        return ModelResponse(content="summary")

    result = await plugin._on_before_context(HookContext(
        stage=HookStage.BEFORE_CONTEXT,
        state={"messages": original},
        session=SimpleNamespace(turn_count=1),
        invoke_model=invoke_model,
    ))

    assert result["compact_reason"] == "automatic"


@pytest.mark.asyncio
async def test_long_mailbox_turn_compacts_without_user_messages():
    plugin = make_plugin()
    await plugin.on_load({"trigger_chars": 1000, "keep_recent_turns": 2})
    original = [Message(role="system", content="Mailbox message: continue goal")]
    for index in range(6):
        original.extend([
            Message(role="assistant", content=f"step {index} " + "x" * 200),
            Message(role="tool", content="result " + "x" * 200),
        ])

    async def invoke_model(messages):
        assert messages[1].role == "system"
        return ModelResponse(content="Earlier goal progress")

    result = await plugin._on_before_context(HookContext(
        stage=HookStage.BEFORE_CONTEXT,
        state={"messages": original},
        session=SimpleNamespace(turn_count=1),
        invoke_model=invoke_model,
    ))

    assert result["compact_reason"] == "automatic"
    assert ET.fromstring(result["messages"][0].content).tag == (
        "conversation_summary"
    )
    assert [message.role for message in result["messages"][1:]] == [
        "assistant", "tool", "assistant", "tool",
    ]


@pytest.mark.asyncio
async def test_zero_provider_usage_uses_character_fallback():
    plugin = make_plugin()
    await plugin.on_load({"trigger_chars": 1000, "keep_recent_turns": 1})
    original = history(3, content="x" * 300)
    original[-1].usage_metadata = {"input_tokens": 0, "output_tokens": 10}

    async def invoke_model(_messages):
        return ModelResponse(content="summary")

    result = await plugin._on_before_context(
        HookContext(
            stage=HookStage.BEFORE_CONTEXT,
            state={"messages": original},
            session=SimpleNamespace(turn_count=4),
            invoke_model=invoke_model,
        )
    )

    assert result["compact_reason"] == "automatic"


@pytest.mark.asyncio
async def test_failed_summary_leaves_history_untouched():
    plugin = make_plugin()
    await plugin.on_load({"automatic": False, "keep_recent_turns": 1})
    plugin._manual_requested = True
    original = history(2)

    async def fail(_messages):
        raise RuntimeError("summary unavailable")

    ctx = HookContext(
        stage=HookStage.BEFORE_CONTEXT,
        state={"messages": original},
        session=SimpleNamespace(turn_count=2),
        invoke_model=fail,
    )

    with pytest.raises(RuntimeError, match="summary unavailable"):
        await plugin._on_before_context(ctx)

    assert ctx.state["messages"] == original
    assert plugin._manual_requested is False
    assert plugin.diagnostics()["compactions"] == 0


@pytest.mark.asyncio
async def test_failed_automatic_summary_continues_with_original_history():
    plugin = make_plugin()
    await plugin.on_load({"trigger_chars": 100, "keep_recent_turns": 1})
    original = history(3, content="x" * 100)

    async def fail(_messages):
        raise ConnectionError("summary provider unavailable")

    ctx = HookContext(
        stage=HookStage.BEFORE_CONTEXT,
        state={"messages": original},
        session=SimpleNamespace(turn_count=2),
        invoke_model=fail,
    )

    assert await plugin._on_before_context(ctx) is None
    assert ctx.state["messages"] == original
    assert plugin.diagnostics()["compactions"] == 0


@pytest.mark.asyncio
async def test_unload_resets_plugin_owned_state():
    plugin = make_plugin()
    plugin._manual_requested = True
    plugin._compactions = 2
    plugin._last_reason = "automatic"

    await plugin.on_unload()

    assert plugin._manual_requested is False
    assert plugin.diagnostics()["compactions"] == 0
    assert plugin.diagnostics()["last_reason"] == ""
    assert plugin.diagnostics()["last_compaction"] == {}


@pytest.mark.asyncio
async def test_compact_tool_rewrites_and_persists_history(
    state_store,
    temp_workspace,
):
    plugin = make_plugin()
    await plugin.on_load({"automatic": False, "keep_recent_turns": 1})
    setup = SetupContext()
    plugin.setup(setup)

    hooks = HookManager()
    hooks.register(HookStage.BEFORE_CONTEXT, setup.hooks[HookStage.BEFORE_CONTEXT])
    registry = ToolRegistry()
    registry.register(
        setup.tool,
        sandbox_mode=setup.options.sandbox_mode,
        namespace=setup.options.namespace,
    )
    state_store.sync_messages(history(2))
    llm = MockLLM(responses=[
        {
            "content": "requesting compact",
            "tool_calls": [{"id": "compact-1", "name": "compact", "args": {}}],
        },
        {"content": "Earlier requirements and outcomes."},
        {"content": "Compaction complete."},
    ])
    engine = Engine(
        llm=llm,
        tool_registry=registry,
        hook_manager=hooks,
        state_store=state_store,
        context_builder=ContextBuilder(),
        sandbox_policy=SandboxPolicy(
            enabled=False,
            workspace_root=str(temp_workspace),
        ),
        permission_system=PermissionSystem(default_decision="allow"),
        config=None,
    )
    await engine.start_session()

    events = [event async for event in engine.run_turn("compact this history")]
    persisted = state_store.read_messages()

    assert llm.call_count == 3
    assert persisted[0].role == "system"
    assert "Earlier requirements and outcomes." in persisted[0].content
    assert [message.role for message in persisted[1:]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert persisted[1].content == "compact this history"
    tool_event = next(event for event in events if event["type"] == "tool_result")
    assert tool_event["data"]["data"] == {"requested": True}
    assert [
        event["data"]["content"]
        for event in events
        if event["type"] == "assistant_message"
    ] == ["requesting compact", "Compaction complete."]

    resumed = Engine(
        llm=MockLLM(responses=[]),
        tool_registry=ToolRegistry(),
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
    await resumed.start_session()

    assert resumed.messages == persisted
