"""Behavior tests for the built-in conversation compaction plugin."""

from XBotv2.tests.helpers import make_engine

import asyncio
import json
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from XBotv2.compact.plugin import (
    CompactPlugin,
    _compact_prefix_end,
    _history_chars,
)
from XBotv2.core import (
    EventContext,
    Events,
    Message,
    ModelResponse,
    ToolCall,
    estimate_request_tokens,
)
from XBotv2.core.tokens import (
    REQUEST_CONTEXT_WINDOW_KEY,
    REQUEST_ESTIMATE_KEY,
)
from XBotv2.context_builder.builder import ContextBuilder
from XBotv2.config.models import RuntimeConfig
from XBotv2.agentloop.engine import Engine
import xcore
from plugin_harness import mount_plugin_standalone
from XBotv2.llm.mock import MockLLM
from XBotv2.permissions.system import PermissionSystem
from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.sandbox.policy import SandboxPolicy


def make_plugin(config=None) -> CompactPlugin:
    from XBotv2.compact.plugin import CompactPlugin

    return mount_plugin_standalone(CompactPlugin(), config)


class SetupContext:
    """Post-apply view of a plugin's registrations on a real XCore context."""

    def __init__(self, plugin) -> None:
        self.ctx = plugin.ctx
        self.tool = None
        self.options = None
        self.commands: dict = {}
        entries = self.ctx.tools.registry.registered_entries()
        if entries:
            entry = entries[0]
            self.tool = entry.tool
            self.options = type(
                "Options", (), {"namespace": entry.namespace}
            )()
        for command in self.ctx.commands.all():
            self.commands[command.name] = command


def history(turns: int, *, content: str = "message") -> list[Message]:
    messages = []
    for index in range(turns):
        messages.extend([
            Message(role="user", content=f"user {index} {content}"),
            Message(role="assistant", content=f"assistant {index} {content}"),
        ])
    return messages


class FailingModel:
    """Provider whose streaming summary call raises the given error."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def astream(self, _messages):
        if False:  # pragma: no cover - keeps this an async generator
            yield None
        raise self._error


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
async def test_commit_dispatches_pre_and_post_compact_bracket():
    """Compaction commit brackets replacement with PRE/POST events."""
    plugin = make_plugin({"automatic": False, "keep_recent_turns": 1})
    setup = SetupContext(plugin)
    plugin.model = MockLLM(responses=[{"content": "Earlier context."}])
    calls = []

    async def pre_compact(ctx):
        calls.append(("pre", str(ctx.event.get("reason")), len(ctx.messages)))

    async def post_compact(ctx):
        calls.append((
            "post",
            str(ctx.event.get("reason")),
            ctx.event.get("previous_message_count"),
            ctx.event.get("current_message_count"),
        ))

    setup.ctx.on(Events.PRE_COMPACT, pre_compact)
    setup.ctx.on(Events.POST_COMPACT, post_compact)
    plugin._manual_requested = True
    original = history(3)
    ctx = EventContext(messages=original, session=SimpleNamespace(turn_count=3))
    result = await plugin._on_before_context(ctx)

    assert result == {"rebuild": True}
    assert calls == [
        ("pre", "manual", 3),
        ("post", "manual", 6, 3),
    ]


@pytest.mark.asyncio
async def test_manual_tool_requests_compaction_below_threshold():
    plugin = make_plugin({"automatic": False, "keep_recent_turns": 1})
    setup = SetupContext(plugin)
    plugin.model = MockLLM(responses=[{"content": "Important earlier context"}])
    tool_result = await setup.tool.ainvoke({})

    original = history(3)
    ctx = EventContext(
        messages=original,
        session=SimpleNamespace(turn_count=3),
    )
    result = await setup.ctx.serial(Events.BEFORE_CONTEXT, ctx)

    request = ET.fromstring(plugin.model.get_call_messages(0)[-1].content)
    assert request.tag == "summary_request"
    assert request.text.strip() == "Produce the conversation summary now."
    assert tool_result.status == "success"
    assert result == {"rebuild": True}
    assert ctx.messages[0].role == "system"
    assert "Important earlier context" in ctx.messages[0].content
    assert ctx.messages[1:] == original[-2:]


@pytest.mark.asyncio
async def test_human_command_compacts_and_persists_immediately(
    caplog,
    state_store,
    temp_workspace,
):
    caplog.set_level("INFO", logger="xbotv2.compact")
    plugin = make_plugin({"automatic": False, "keep_recent_turns": 1})
    setup = SetupContext(plugin)
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
    engine = make_engine(
        llm=llm,
        tool_registry=ToolRegistry(),
        plugin_ctx=setup.ctx,
        state_store=state_store,
        context_builder=ContextBuilder(),
        sandbox_policy=SandboxPolicy(
            enabled=False,
            workspace_root=str(temp_workspace),
        ),
        permission_system=PermissionSystem(default_decision="allow"),
        config=RuntimeConfig(),
    )
    setup.ctx.model.replace(llm)
    engine.state.messages = list(original)
    engine.state.session.turn_count = 3
    from XBotv2.persistence.plugin import PersistenceService

    persistence = PersistenceService(state_store, engine.state)
    setup.ctx.on(Events.STATE_CHANGED, persistence.state_changed)
    await engine.start_session()
    runtime_events = []

    def record_runtime_event(event: EventContext) -> None:
        runtime_events.append(event.client_event or {})

    setup.ctx.on(Events.RUNTIME_EVENT, record_runtime_event)
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
    assert any(
        any(
            part.get("type") == "text"
            and part.get("text") == "user 0 message"
            for part in record.get("parts", [])
        )
        for record in records
    )

    assert result.status == "ok"
    assert result.data.get("requested") is True
    assert result.data.get("compacted") is True
    history_chars_before = _history_chars(original)
    history_chars_after = _history_chars(engine.messages)
    # Compaction reduces message count; character count may not always decrease
    # when a system summary is prepended.
    assert len(engine.messages) < len(original)
    assert "context tokens" in result.message
    assert "30 input and 4 output tokens" in result.message
    assert [event["type"] for event in runtime_events] == [
        "compaction_started",
        "compaction_completed",
    ]
    assert "context tokens" in result.message
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
    plugin = make_plugin({"automatic": False, "keep_recent_turns": 1})
    setup = SetupContext(plugin)
    plugin.model = MockLLM(responses=[{"content": "Earlier context."}])
    turn_lock = asyncio.Lock()
    await turn_lock.acquire()

    class EngineStub:
        messages = history(3)
        session = SimpleNamespace(turn_count=3)
        settings = SimpleNamespace(
            max_context_tokens=32_000,
            max_output_tokens=None,
        )

    command_ctx = SimpleNamespace(turn_lock=turn_lock, engine=EngineStub())

    command_task = asyncio.create_task(
        setup.commands["compact"].handler(command_ctx, "")
    )
    await asyncio.sleep(0)

    assert command_task.done() is False

    turn_lock.release()
    result = await command_task

    assert result.status == "ok"
    assert plugin.diagnostics()["compactions"] == 1
    assert turn_lock.locked() is False


@pytest.mark.asyncio
async def test_compaction_does_not_append_duplicate_human_directives():
    plugin = make_plugin({"automatic": False, "keep_recent_turns": 1})
    plugin._manual_requested = True
    original = history(3)
    original[2].content = "Do not ask me again; decide the safest option."

    plugin.model = MockLLM(responses=[{
        "content": "## Conversation Summary\n\nOlder context only."
    }])

    ctx = EventContext(
        messages=original,
        session=SimpleNamespace(turn_count=3),
    )
    result = await plugin._on_before_context(ctx)

    summary = ctx.messages[0].content
    root = ET.fromstring(summary)
    assert root.tag == "historical_context"
    assert root.attrib == {"source": "compaction"}
    assert root.find("conversation_summary") is not None
    assert "## Recent Human Directives (verbatim)" not in summary
    assert summary.count("Older context only.") == 1


@pytest.mark.asyncio
async def test_large_context_does_not_use_fixed_character_threshold():
    plugin = make_plugin({"keep_recent_turns": 1})
    original = history(3, content="x" * 13_500)
    context = [Message(role="system", content="x" * 80_000), *original]

    result = await plugin._on_before_model_request(EventContext(
        messages=original,
        model_request={"messages": context, "tools": []},
        config=SimpleNamespace(
            max_context_tokens=1_048_576,
            max_output_tokens=None,
        ),
        session=SimpleNamespace(turn_count=3),
    ))

    assert result is None


@pytest.mark.asyncio
async def test_automatic_threshold_uses_provider_window_and_output_limit():
    plugin = make_plugin({"keep_recent_turns": 1})
    original = history(3, content="x" * 5_000)
    context = [Message(role="system", content="stable"), *original]
    request_estimate = estimate_request_tokens(context)
    original[-1].response_metadata[REQUEST_ESTIMATE_KEY] = request_estimate
    original[-1].response_metadata[REQUEST_CONTEXT_WINDOW_KEY] = 200_000
    original[-1].usage_metadata["context_tokens"] = 136_000

    plugin.model = MockLLM(responses=[{"content": (
        "## Requirements\nKeep constraints.\n\n"
        "## Decisions\nUse evidence.\n\n"
        "## Current State\nOlder work done.\n\n"
        "## Remaining Work\nContinue."
    )}])

    result = await plugin._on_before_model_request(EventContext(
        messages=original,
        model_request={"messages": context, "tools": []},
        config=SimpleNamespace(
            max_context_tokens=200_000,
            max_output_tokens=64_000,
        ),
        session=SimpleNamespace(turn_count=3),
    ))

    sent = plugin.model.get_call_messages(0)
    assert sent[0].content == "stable"
    assert ET.fromstring(sent[1].content).tag == "summary_instructions"
    assert result == {"rebuild": True}
    assert plugin._last_compaction["context_limit"] == 136_000
    assert plugin._last_compaction["estimate_source"] == "provider_calibrated"
    assert plugin._last_compaction["context_tokens_after_estimate"] < 136_000


@pytest.mark.asyncio
async def test_automatic_compaction_preserves_recent_tool_iterations():
    plugin = make_plugin({"keep_recent_turns": 2, "trigger_ratio": 0.01})
    original = [Message(role="system", content="Goal continuation")]
    for index in range(6):
        call_id = f"call-{index}"
        original.extend([
            Message(
                role="assistant",
                content=f"step {index}",
                tool_calls=[ToolCall(call_id, "echo", {"value": index})],
            ),
            Message(role="tool", content=f"result {index}", tool_call_id=call_id),
        ])

    plugin.model = MockLLM(responses=[{"content": (
        "## Requirements\nContinue goal.\n\n"
        "## Decisions\nNone.\n\n"
        "## Current State\nFour steps summarized.\n\n"
        "## Remaining Work\nTwo steps remain."
    )}])

    ctx = EventContext(
        messages=original,
        model_request={
            "messages": [Message(role="system", content="stable"), *original],
            "tools": [],
        },
        config=SimpleNamespace(
            max_context_tokens=100,
            max_output_tokens=None,
        ),
        session=SimpleNamespace(turn_count=1),
    )
    result = await plugin._on_before_model_request(ctx)

    assert [message.role for message in ctx.messages[1:]] == [
        "assistant", "tool", "assistant", "tool",
    ]


@pytest.mark.asyncio
async def test_failed_summary_leaves_history_untouched():
    plugin = make_plugin({"automatic": False, "keep_recent_turns": 1})
    plugin._manual_requested = True
    original = history(2)

    plugin.model = FailingModel(RuntimeError("summary unavailable"))

    ctx = EventContext(
        messages=original,
        session=SimpleNamespace(turn_count=2),
    )

    with pytest.raises(RuntimeError, match="summary unavailable"):
        await plugin._on_before_context(ctx)

    assert ctx.messages == original
    assert plugin._manual_requested is False
    assert plugin.diagnostics()["compactions"] == 0


@pytest.mark.asyncio
async def test_failed_automatic_summary_continues_with_original_history():
    plugin = make_plugin({"trigger_ratio": 0.1, "keep_recent_turns": 1})
    original = history(3, content="x" * 1_000)

    plugin.model = FailingModel(ConnectionError("summary provider unavailable"))

    ctx = EventContext(
        messages=original,
        model_request={
            "messages": [Message(role="system", content="stable"), *original],
            "tools": [],
        },
        config=SimpleNamespace(
            max_context_tokens=1_000,
            max_output_tokens=None,
        ),
        session=SimpleNamespace(turn_count=2),
    )

    assert await plugin._on_before_model_request(ctx) is None
    assert ctx.messages == original
    assert plugin.diagnostics()["compactions"] == 0


@pytest.mark.asyncio
async def test_unload_resets_plugin_owned_state():
    plugin = make_plugin()
    plugin._manual_requested = True
    plugin._compactions = 2
    plugin._last_reason = "automatic"

    await plugin._on_unload()

    assert plugin._manual_requested is False
    assert plugin.diagnostics()["compactions"] == 0
    assert plugin.diagnostics()["last_reason"] == ""
    assert plugin.diagnostics()["last_compaction"] == {}


@pytest.mark.asyncio
async def test_compact_tool_rewrites_and_persists_history(
    state_store,
    temp_workspace,
):
    plugin = make_plugin({"automatic": False, "keep_recent_turns": 1})
    setup = SetupContext(plugin)

    registry = ToolRegistry()
    registry.register(
        setup.tool,
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
    engine = make_engine(
        llm=llm,
        tool_registry=registry,
        plugin_ctx=setup.ctx,
        state_store=state_store,
        context_builder=ContextBuilder(),
        sandbox_policy=SandboxPolicy(
            enabled=False,
            workspace_root=str(temp_workspace),
        ),
        permission_system=PermissionSystem(default_decision="allow"),
        config=RuntimeConfig(),
    )
    setup.ctx.model.replace(llm)
    engine.state.messages = list(state_store.read_messages())
    from XBotv2.persistence.plugin import PersistenceService

    persistence = PersistenceService(state_store, engine.state)
    setup.ctx.on(Events.STATE_CHANGED, persistence.state_changed)
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
    # The compact tool result no longer carries a ``data`` field.
    assert tool_event["data"]["content"]
    assert [
        event["data"]["content"]
        for event in events
        if event["type"] == "assistant_message"
    ] == ["requesting compact", "Compaction complete."]

    resumed = make_engine(
        llm=MockLLM(responses=[]),
        tool_registry=ToolRegistry(),
        plugin_ctx=xcore.Context(),
        state_store=state_store,
        context_builder=ContextBuilder(),
        sandbox_policy=SandboxPolicy(
            enabled=False,
            workspace_root=str(temp_workspace),
        ),
        permission_system=PermissionSystem(default_decision="allow"),
        config=RuntimeConfig(),
    )
    if state_store.has_existing_session():
        messages = state_store.read_messages()
        resumed.state.messages = messages
        resumed.state.turn_count = sum(
            1 for message in messages if message.role == "user"
        )
        resumed.state.resumed = True
    await resumed.start_session()

    assert resumed.messages == persisted


@pytest.mark.asyncio
async def test_automatic_compaction_rebuilds_context_before_provider_call(
    state_store,
    temp_workspace,
):
    plugin = make_plugin({"keep_recent_turns": 1, "trigger_ratio": 0.5})
    setup = SetupContext(plugin)
    state_store.sync_messages(history(3, content="x" * 5_000))
    llm = MockLLM(responses=[
        {"content": (
            "## Requirements\nPreserve the request.\n\n"
            "## Decisions\nKeep recent work.\n\n"
            "## Current State\nOlder work summarized.\n\n"
            "## Remaining Work\nAnswer the user."
        )},
        {
            "content": "Done",
            "usage_metadata": {
                "input_tokens": 2_000,
                "output_tokens": 4,
                "context_tokens": 2_000,
            },
        },
    ])
    engine = make_engine(
        llm=llm,
        tool_registry=ToolRegistry(),
        plugin_ctx=setup.ctx,
        state_store=state_store,
        context_builder=ContextBuilder(),
        sandbox_policy=SandboxPolicy(
            enabled=False,
            workspace_root=str(temp_workspace),
        ),
        permission_system=PermissionSystem(default_decision="allow"),
        config=RuntimeConfig(max_context_tokens=10_000),
    )
    setup.ctx.model.replace(llm)
    engine.state.messages = list(state_store.read_messages())
    from XBotv2.persistence.plugin import PersistenceService

    persistence = PersistenceService(state_store, engine.state)
    setup.ctx.on(Events.STATE_CHANGED, persistence.state_changed)
    await engine.start_session()

    events = [event async for event in engine.run_turn("continue")]

    assert llm.call_count == 2
    assert any(event["type"] == "assistant_message" for event in events)
    assert engine.messages[0].role == "system"
    root = ET.fromstring(engine.messages[0].content)
    assert root.tag == "historical_context"
    assert root.find("conversation_summary") is not None
    assert (
        plugin._last_compaction["context_tokens_after_estimate"]
        < plugin._last_compaction["context_tokens_before"]
    )
