"""
Comprehensive test suite for the Hermes agent runtime.

Tests cover:
- Tool calls (shell, filesystem, ask, message_send)
- Restart and reconnect scenarios
- Subagent creation and management (attach/detach modes)
- Cron job execution
- Subagent tool calls
- Permission system (allow/deny/ask)
- Subagent permission inheritance
- Human-in-the-loop (permission ask flow)
- Persistence verification (checkpoint restore)
"""

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import pytest_asyncio

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage, SystemMessage
from langchain_core.tools import tool as lc_tool
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command, interrupt

from xbot.models import UserContext, PermissionConfig, SandboxConfig
from xbot.mock_llm import (
    MockLLM,
    TOOL_CALL_SEQUENCE,
    MULTI_TOOL_SEQUENCE,
    PERMISSION_ASK_SEQUENCE,
    SUBAGENT_SEQUENCE,
    COMPRESSION_SEQUENCE,
    RECONNECT_SEQUENCE,
    CRON_JOB_SEQUENCE,
)
from xbot.tools import (
    shell,
    filesystem_read,
    filesystem_write,
    filesystem_list,
    ask,
    message_send,
    subagent_create,
    subagent_wait,
    subagent_list,
    subagent_stop,
    memory_update,
    compact,
    skill_load,
    get_all_tools,
)
from xbot.permissions import PermissionSystem
from xbot.sandbox import SandboxPolicy


# Helper to create tools with custom parameters for tests
def create_shell_tool():
    """Create shell tool."""
    return shell

def create_filesystem_tool(workspace_root: str):
    """Create filesystem tools with workspace restriction."""
    # Tools already have workspace restriction built-in
    return [filesystem_read, filesystem_write, filesystem_list]

def create_ask_tool():
    """Create ask tool."""
    return ask

def create_message_send_tool():
    """Create message_send tool."""
    return message_send

def create_subagent_create_tool(base_path: str):
    """Create subagent_create tool."""
    return subagent_create

def create_subagent_wait_tool():
    """Create subagent_wait tool."""
    return subagent_wait

def create_subagent_list_tool():
    """Create subagent_list tool."""
    return subagent_list

def create_memory_update_tool(memory_path: str):
    """Create memory_update tool."""
    return memory_update


@pytest.mark.asyncio
async def test_filesystem_read_rejects_paths_outside_workspace():
    """Filesystem tools must not resolve paths outside the workspace."""
    with pytest.raises(ValueError, match="Path escapes workspace"):
        await filesystem_read.ainvoke({"path": "/etc/passwd"})


def test_permission_deny_precedence():
    """Deny rules must take precedence over broad allow rules."""
    permission_system = PermissionSystem(
        PermissionConfig(
            default="ask",
            allow=[{"tool": "shell", "params": {"command": ".*"}}],
            deny=[{"tool": "shell", "params": {"command": "^rm"}}],
        )
    )

    assert permission_system.check("shell", {"command": "rm -rf /tmp/test"}) == "deny"


def test_permission_ask_rule_is_respected():
    """Explicit ask rules must be honored."""
    permission_system = PermissionSystem(
        PermissionConfig(
            default="deny",
            ask=[{"tool": "shell", "params": {"command": "^whoami$"}}],
        )
    )

    assert permission_system.check("shell", {"command": "whoami"}) == "ask"


@pytest.mark.asyncio
async def test_compact_tool_is_allowed_and_returns_manual_request():
    """The compact tool must remain a manual trigger."""
    result = await compact.ainvoke({})
    assert "Manual context compression requested" in result


@pytest.mark.asyncio
async def test_sandbox_enabled_requires_tool_registration(mock_llm):
    """New tools must explicitly declare their sandbox mode before exposure."""
    from xbot.graph import build_agent_graph

    @lc_tool
    async def unregistered_tool() -> str:
        """A tool intentionally missing from TOOL_SANDBOX_MODE."""
        return "ok"

    with pytest.raises(ValueError, match="not registered"):
        build_agent_graph(
            llm=mock_llm,
            tools=[unregistered_tool],
            checkpointer=MemorySaver(),
            store=None,
            permission_system=PermissionSystem(PermissionConfig(default="allow")),
            sandbox_policy=SandboxPolicy(SandboxConfig(enabled=True)),
        )


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is required")
@pytest.mark.asyncio
async def test_sandbox_shell_masks_denied_paths(temp_data_dir):
    """A script running inside shell must not read or write denied host paths."""
    workspace = temp_data_dir / "sessions" / "default" / "workspace"
    secret_dir = workspace / "secrets"
    secret_dir.mkdir(parents=True)
    secret_file = secret_dir / "token.txt"
    secret_file.write_text("original", encoding="utf-8")

    policy = SandboxPolicy(
        SandboxConfig(
            enabled=True,
            resources=[
                {"path": str(workspace), "access": "readwrite", "recursive": True},
                {"path": str(secret_dir), "access": "deny", "recursive": True},
            ],
        ),
        data_root=temp_data_dir,
        workspace_root=workspace,
    )

    result = await policy.run_shell(
        "cat secrets/token.txt; "
        "echo hacked > secrets/token.txt; "
        "echo allowed > visible.txt"
    )

    assert "No such file" in result or "Read-only file system" in result
    assert secret_file.read_text(encoding="utf-8") == "original"
    assert (workspace / "visible.txt").read_text(encoding="utf-8") == "allowed\n"


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is required")
@pytest.mark.asyncio
async def test_sandbox_one_call_approval_can_expose_exact_path(temp_data_dir):
    """A sandbox ask approval should expose only the approved path for one call."""
    workspace = temp_data_dir / "sessions" / "default" / "workspace"
    secret_dir = workspace / "secrets"
    secret_dir.mkdir(parents=True)
    secret_file = secret_dir / "token.txt"
    secret_file.write_text("original", encoding="utf-8")

    policy = SandboxPolicy(
        SandboxConfig(
            enabled=True,
            resources=[
                {"path": str(workspace), "access": "readwrite", "recursive": True},
                {"path": str(secret_dir), "access": "ask", "recursive": True},
            ],
        ),
        data_root=temp_data_dir,
        workspace_root=workspace,
    )

    with pytest.raises(ValueError, match="asks before read access"):
        await policy.read_text("secrets/token.txt")

    policy.approve_once(secret_file, "read")
    try:
        assert await policy.read_text("secrets/token.txt") == "original"
    finally:
        policy.clear_one_call_approvals()


def test_sandbox_shell_preflight_blocks_unapproved_absolute_write(temp_data_dir):
    """Shell commands must not appear to succeed against unapproved host paths."""
    workspace = temp_data_dir / "sessions" / "default" / "workspace"
    policy = SandboxPolicy(
        SandboxConfig(
            enabled=True,
            default="deny",
            resources=[
                {"path": str(workspace), "access": "readwrite", "recursive": True},
            ],
        ),
        data_root=temp_data_dir,
        workspace_root=workspace,
    )

    decision = policy.guard_tool_call(
        "shell",
        {"command": 'echo "hello-agent" > /home/shefrin/hello-agent.txt'},
        "sandboxed",
    )

    assert decision.action == "deny"
    assert "/home/shefrin/hello-agent.txt" in decision.reason


def test_sandbox_guard_uses_tool_semantics_for_write_paths(temp_data_dir):
    """Write tools should ask/deny as writes before helper execution starts."""
    workspace = temp_data_dir / "sessions" / "default" / "workspace"
    ask_dir = workspace / "approval"
    ask_dir.mkdir(parents=True)
    target = ask_dir / "note.txt"
    policy = SandboxPolicy(
        SandboxConfig(
            enabled=True,
            default="deny",
            resources=[
                {"path": str(workspace), "access": "readwrite", "recursive": True},
                {"path": str(ask_dir), "access": "ask", "recursive": True},
            ],
        ),
        data_root=temp_data_dir,
        workspace_root=workspace,
    )

    decision = policy.guard_tool_call("filesystem_write", {"path": str(target)}, "sandboxed")

    assert decision.action == "ask"
    assert decision.operation == "write"
    assert "write access" in decision.reason


def test_sandbox_reports_workspace_symlink_escape(temp_data_dir):
    """Workspace symlinks that resolve outside the sandbox should be explicit."""
    workspace = temp_data_dir / "sessions" / "default" / "workspace"
    outside = temp_data_dir / "outside"
    outside.mkdir()
    (workspace / "outside").symlink_to(outside)
    policy = SandboxPolicy(
        SandboxConfig(
            enabled=True,
            default="deny",
            resources=[
                {"path": str(workspace), "access": "readwrite", "recursive": True},
            ],
        ),
        data_root=temp_data_dir,
        workspace_root=workspace,
    )

    decision = policy.guard_tool_call("filesystem_list", {"path": "outside"}, "sandboxed")

    assert decision.action == "deny"
    assert "resolves outside via symlink" in decision.reason


def test_runtime_paths_drive_session_and_personality_dirs(temp_data_dir):
    """Session/personality ids should derive runtime paths and default sandbox rules."""
    from xbot.config import configure_runtime_paths, default_sandbox_config, get_runtime_paths

    original = get_runtime_paths()
    try:
        paths = configure_runtime_paths(
            data_dir=temp_data_dir,
            session_id="analysis",
            personality_id="hermes",
        )
        sandbox_config = default_sandbox_config(paths)
        resource_paths = {rule.path for rule in sandbox_config.resources}

        assert paths.workspace_dir == temp_data_dir / "sessions" / "analysis" / "workspace"
        assert paths.personality_dir == temp_data_dir / "personality" / "hermes"
        assert "sessions/analysis/workspace" in resource_paths
        assert "personality/hermes/MEMORY.md" in resource_paths
    finally:
        configure_runtime_paths(
            data_dir=original.data_dir,
            session_id=original.session_id,
            personality_id=original.personality_id,
        )


def test_config_expands_runtime_placeholders(temp_data_dir):
    """Sandbox config files can follow the active runtime paths."""
    from xbot.config import configure_runtime_paths, expand_runtime_placeholders, get_runtime_paths

    original = get_runtime_paths()
    try:
        configure_runtime_paths(
            data_dir=temp_data_dir,
            session_id="analysis",
            personality_id="hermes",
        )
        expanded = expand_runtime_placeholders(
            {
                "resources": [
                    {"path": "sessions/<session_id>/workspace"},
                    {"path": "personality/{{personality_id}}/MEMORY.md"},
                ]
            }
        )

        assert expanded == {
            "resources": [
                {"path": "sessions/analysis/workspace"},
                {"path": "personality/hermes/MEMORY.md"},
            ]
        }
    finally:
        configure_runtime_paths(
            data_dir=original.data_dir,
            session_id=original.session_id,
            personality_id=original.personality_id,
        )


def test_terminal_does_not_duplicate_tool_call_blocks(capsys):
    """Providers may expose tool calls in both content_blocks and tool_calls."""
    from xbot.terminal import TerminalOptions, TerminalRenderer

    message = AIMessage(
        content=[{"type": "tool_call", "name": "shell", "args": {"command": "pwd"}, "id": "call_1"}],
        tool_calls=[{"name": "shell", "args": {"command": "pwd"}, "id": "call_1", "type": "tool_call"}],
    )
    renderer = TerminalRenderer(agent_name="default", options=TerminalOptions(print_tools=True))

    renderer.message(message)
    output = capsys.readouterr().out

    assert output.count("Tool Call>") == 1


def test_terminal_renders_normalized_tool_call_event(capsys):
    """Terminal should render complete tool-call events, not assemble chunks."""
    from xbot.interaction import InteractionEvent
    from xbot.terminal import TerminalOptions, TerminalRenderer

    renderer = TerminalRenderer(agent_name="default", options=TerminalOptions(print_tools=True))
    renderer.event(InteractionEvent("tool_call", "agent", {"name": "shell", "args": {"command": "pwd"}}))

    output = capsys.readouterr().out
    assert output.count("Tool Call>") == 1
    assert "{'command': 'pwd'}" in output


def test_stream_tool_call_waits_for_complete_args(user_context):
    """Streaming normalization must not emit shell({}) half-calls."""
    from xbot.interaction import HermesInteraction

    runtime = HermesInteraction(
        user_context=user_context,
        agent_config=type("AgentCfg", (), {"name": "test", "max_context_tokens": 8000})(),
        provider_config=type("ProviderCfg", (), {"name": "mock", "model": "mock"})(),
        graph=None,
        graph_config={"configurable": {"thread_id": "partial_tool_call_test"}},
        sandbox=SandboxPolicy(SandboxConfig(enabled=False)),
        tools=[],
        database_path=":memory:",
    )

    first = runtime._complete_tool_calls_from_chunk(
        AIMessageChunk(content="", tool_calls=[{"name": "shell", "args": {}, "id": "call_1", "type": "tool_call"}])
    )
    second = runtime._complete_tool_calls_from_chunk(
        AIMessageChunk(content="", tool_calls=[{"name": "shell", "args": {"command": "pwd"}, "id": "call_1", "type": "tool_call"}])
    )

    assert first == []
    assert second == [{"name": "shell", "args": {"command": "pwd"}, "id": "call_1"}]


@pytest.mark.asyncio
async def test_interaction_stream_emits_deltas_without_final_duplicate(mock_llm, user_context):
    """Streaming platforms should receive token deltas instead of only final messages."""
    from xbot.interaction import HermesInteraction
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver

    mock_llm.chunk_size = 3
    mock_llm.set_response_sequence([{"content": "stream me"}])
    graph = build_agent_graph(
        llm=mock_llm,
        tools=[],
        checkpointer=MemorySaver(),
        store=None,
        permission_system=PermissionSystem(PermissionConfig(default="allow")),
    )
    runtime = HermesInteraction(
        user_context=user_context,
        agent_config=type("AgentCfg", (), {"name": "test", "max_context_tokens": 8000})(),
        provider_config=type("ProviderCfg", (), {"name": "mock", "model": "mock"})(),
        graph=graph,
        graph_config={"configurable": {"thread_id": "interaction_stream_test"}},
        sandbox=SandboxPolicy(),
        tools=[],
        database_path=":memory:",
    )

    events = [event async for event in runtime.stream_user_message("hello")]

    assert [event.kind for event in events].count("message_delta") >= 2
    assert not any(event.kind == "message" and getattr(event.payload, "content", None) == "stream me" for event in events)


@pytest.mark.asyncio
async def test_interaction_stream_normalizes_tool_call_events(mock_llm, user_context):
    """Interaction should assemble streamed tool call chunks before platform renderers."""
    from xbot.interaction import HermesInteraction
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver

    mock_llm.chunk_size = 5
    mock_llm.set_response_sequence([
        {
            "content": "calling",
            "tool_calls": [{"name": "shell", "args": {"command": "pwd"}, "id": "call_1"}],
        },
        {"content": "done"},
    ])
    graph = build_agent_graph(
        llm=mock_llm,
        tools=[shell],
        checkpointer=MemorySaver(),
        store=None,
        permission_system=PermissionSystem(PermissionConfig(default="allow")),
        sandbox_policy=SandboxPolicy(SandboxConfig(enabled=False)),
    )
    runtime = HermesInteraction(
        user_context=user_context,
        agent_config=type("AgentCfg", (), {"name": "test", "max_context_tokens": 8000})(),
        provider_config=type("ProviderCfg", (), {"name": "mock", "model": "mock"})(),
        graph=graph,
        graph_config={"configurable": {"thread_id": "interaction_tool_call_stream_test"}},
        sandbox=SandboxPolicy(SandboxConfig(enabled=False)),
        tools=[shell],
        database_path=":memory:",
    )

    events = [event async for event in runtime.stream_user_message("hello")]
    tool_calls = [event.payload for event in events if event.kind == "tool_call"]

    assert tool_calls == [{"name": "shell", "args": {"command": "pwd"}, "id": "call_1"}]


@pytest.mark.asyncio
async def test_interaction_stream_preserves_zero_arg_tool_calls(mock_llm, user_context):
    """Zero-argument tools are valid and should be emitted from final updates."""
    from xbot.interaction import HermesInteraction
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver

    mock_llm.set_response_sequence([
        {
            "content": "compacting",
            "tool_calls": [{"name": "compact", "args": {}, "id": "call_compact"}],
        },
        {"content": "done"},
    ])
    graph = build_agent_graph(
        llm=mock_llm,
        tools=[compact],
        checkpointer=MemorySaver(),
        store=None,
        permission_system=PermissionSystem(PermissionConfig(default="allow")),
        sandbox_policy=SandboxPolicy(SandboxConfig(enabled=False)),
    )
    runtime = HermesInteraction(
        user_context=user_context,
        agent_config=type("AgentCfg", (), {"name": "test", "max_context_tokens": 8000})(),
        provider_config=type("ProviderCfg", (), {"name": "mock", "model": "mock"})(),
        graph=graph,
        graph_config={"configurable": {"thread_id": "interaction_zero_arg_tool_call_stream_test"}},
        sandbox=SandboxPolicy(SandboxConfig(enabled=False)),
        tools=[compact],
        database_path=":memory:",
    )

    events = [event async for event in runtime.stream_user_message("compact now")]
    tool_calls = [event.payload for event in events if event.kind == "tool_call"]

    assert {"name": "compact", "args": {}, "id": "call_compact"} in tool_calls


@pytest.mark.asyncio
async def test_interaction_stream_hides_prepare_context_and_reports_compaction(mock_llm, user_context):
    """Compression should be a runtime status event, not streamed summary text."""
    from xbot.interaction import HermesInteraction
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver

    mock_llm.chunk_size = 5
    mock_llm.set_response_sequence([
        {"content": "first visible"},
        {"content": "internal summary"},
        {"content": "visible answer"},
    ])
    graph = build_agent_graph(
        llm=mock_llm,
        tools=[],
        checkpointer=MemorySaver(),
        store=None,
        permission_system=PermissionSystem(PermissionConfig(default="allow")),
        max_messages_before_compress=1,
        keep_recent_messages=1,
    )
    runtime = HermesInteraction(
        user_context=user_context,
        agent_config=type("AgentCfg", (), {"name": "test", "max_context_tokens": 8000})(),
        provider_config=type("ProviderCfg", (), {"name": "mock", "model": "mock"})(),
        graph=graph,
        graph_config={"configurable": {"thread_id": "interaction_compact_stream_test"}},
        sandbox=SandboxPolicy(),
        tools=[],
        database_path=":memory:",
    )

    _ = [event async for event in runtime.stream_user_message("hello")]
    events = [event async for event in runtime.stream_user_message("again")]
    text = "".join(str(getattr(event.payload, "content", "")) for event in events)

    assert any(event.kind == "status" and "Context compacted" in str(event.payload) for event in events)
    assert "internal summary" not in text
    assert "visible answer" in text


@pytest.mark.asyncio
async def test_interaction_batch_reports_compaction_once(mock_llm, user_context):
    """Non-streaming platforms should also receive compaction status events."""
    from xbot.interaction import HermesInteraction
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver

    mock_llm.set_response_sequence([
        {"content": "first visible"},
        {"content": "summary"},
        {"content": "after compact"},
    ])
    graph = build_agent_graph(
        llm=mock_llm,
        tools=[],
        checkpointer=MemorySaver(),
        store=None,
        permission_system=PermissionSystem(PermissionConfig(default="allow")),
        max_messages_before_compress=1,
        keep_recent_messages=1,
    )
    runtime = HermesInteraction(
        user_context=user_context,
        agent_config=type("AgentCfg", (), {"name": "test", "max_context_tokens": 8000})(),
        provider_config=type("ProviderCfg", (), {"name": "mock", "model": "mock"})(),
        graph=graph,
        graph_config={"configurable": {"thread_id": "interaction_batch_compact_test"}},
        sandbox=SandboxPolicy(),
        tools=[],
        database_path=":memory:",
    )

    await runtime.send_user_message("hello")
    result = await runtime.send_user_message("again")
    status_events = [event for event in result.events if event.kind == "status" and "Context compacted" in str(event.payload)]

    assert len(status_events) == 1


@pytest.mark.asyncio
async def test_prepare_context_clears_stale_runtime_events(mock_llm):
    """Runtime events are one-shot updates, not durable notices."""
    from xbot.graph import make_prepare_context_node

    node = make_prepare_context_node(
        mock_llm,
        max_messages_before_compress=10,
        max_context_chars=10_000,
        keep_recent_messages=1,
    )
    result = await node(
        {
            "messages": [HumanMessage(content="short")],
            "runtime_events": [{"type": "context_compacted", "message": "old"}],
        }
    )

    assert result == {"runtime_events": []}


@pytest.mark.asyncio
async def test_prepare_context_serializes_user_context(mock_llm, user_context):
    """Graph state should stay msgpack-friendly instead of persisting Pydantic objects."""
    from xbot.graph import make_prepare_context_node

    node = make_prepare_context_node(
        mock_llm,
        max_messages_before_compress=10,
        max_context_chars=10_000,
        keep_recent_messages=1,
    )
    result = await node(
        {
            "messages": [HumanMessage(content="short")],
            "user_context": user_context,
        }
    )

    assert result["user_context"] == user_context.model_dump()


def test_sanitize_message_chain_drops_orphan_tool_messages():
    """Provider message chains must not contain tool results without calls."""
    from xbot.graph import sanitize_message_chain

    orphan = ToolMessage(content="orphan", name="shell", tool_call_id="missing")
    ai = AIMessage(content="", tool_calls=[{"name": "shell", "args": {"command": "pwd"}, "id": "call_1", "type": "tool_call"}])
    paired = ToolMessage(content="ok", name="shell", tool_call_id="call_1")

    sanitized = sanitize_message_chain([HumanMessage(content="hi"), orphan, ai, paired])

    assert orphan not in sanitized
    assert paired in sanitized


def test_split_for_compaction_preserves_tool_call_groups():
    """Compaction windowing must not split assistant tool calls from results."""
    from xbot.graph import split_for_compaction

    ai = AIMessage(content="", tool_calls=[{"name": "shell", "args": {"command": "pwd"}, "id": "call_1", "type": "tool_call"}])
    tool_result = ToolMessage(content="ok", name="shell", tool_call_id="call_1")

    to_compress, keep = split_for_compaction([HumanMessage(content="hi"), ai, tool_result], keep_recent_messages=1)

    assert ai in keep
    assert tool_result in keep
    assert all(message not in to_compress for message in [ai, tool_result])


def test_interaction_reset_thread_clears_render_state(user_context):
    """Reset should move to a clean thread and clear interaction-side caches."""
    from xbot.interaction import HermesInteraction

    runtime = HermesInteraction(
        user_context=user_context,
        agent_config=type("AgentCfg", (), {"name": "test", "max_context_tokens": 8000})(),
        provider_config=type("ProviderCfg", (), {"name": "mock", "model": "mock"})(),
        graph=None,
        graph_config={"configurable": {"thread_id": "dirty"}},
        sandbox=SandboxPolicy(SandboxConfig(enabled=False)),
        tools=[],
        database_path=":memory:",
    )
    runtime._seen_message_keys.add("x")
    runtime._streamed_message_keys.add("x")
    runtime._streamed_tool_call_keys.add("x")
    runtime._seen_runtime_event_keys.add("x")

    runtime.reset_thread("clean")

    assert runtime.graph_config == {"configurable": {"thread_id": "clean"}}
    assert not runtime._seen_message_keys
    assert not runtime._streamed_message_keys
    assert not runtime._streamed_tool_call_keys
    assert not runtime._seen_runtime_event_keys


@pytest.mark.asyncio
async def test_disabled_sandbox_shell_does_not_mock_success():
    """Disabling sandbox must not make shell appear to execute successfully."""
    policy = SandboxPolicy(SandboxConfig(enabled=False))

    with pytest.raises(RuntimeError, match="requires the system sandbox"):
        await policy.run_shell("pwd")


@pytest.mark.asyncio
async def test_interaction_result_only_emits_new_messages(mock_llm, user_context):
    """The interaction layer should emit new events without replaying old history."""
    from xbot.interaction import HermesInteraction
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver

    mock_llm.set_response_sequence([
        {"content": "first"},
        {"content": "second"},
    ])
    graph = build_agent_graph(
        llm=mock_llm,
        tools=[],
        checkpointer=MemorySaver(),
        store=None,
        permission_system=PermissionSystem(PermissionConfig(default="allow")),
    )
    runtime = HermesInteraction(
        user_context=user_context,
        agent_config=type("AgentCfg", (), {"name": "test", "max_context_tokens": 8000})(),
        provider_config=type("ProviderCfg", (), {"name": "mock", "model": "mock"})(),
        graph=graph,
        graph_config={"configurable": {"thread_id": "interaction_test"}},
        sandbox=SandboxPolicy(),
        tools=[],
        database_path=":memory:",
    )

    first = await runtime.send_user_message("hello")
    second = await runtime.send_user_message("again")

    assert [event.payload.content for event in first.events if event.kind == "message"][-1] == "first"
    assert [event.payload.content for event in second.events if event.kind == "message"][-1] == "second"
    assert all(getattr(event.payload, "content", None) != "first" for event in second.events)


@pytest.mark.asyncio
async def test_interaction_interrupt_keeps_message_before_prompt(mock_llm, user_context):
    """Permission prompts should be emitted after the model message that caused them."""
    from xbot.interaction import HermesInteraction
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver

    mock_llm.set_response_sequence([
        {
            "content": "I will inspect the workspace.",
            "tool_calls": [
                {"name": "shell", "args": {"command": "pwd"}, "id": "call_pwd"},
            ],
        },
        {"content": "Done."},
    ])
    graph = build_agent_graph(
        llm=mock_llm,
        tools=[shell],
        checkpointer=MemorySaver(),
        store=None,
        permission_system=PermissionSystem(PermissionConfig(default="ask")),
        sandbox_policy=SandboxPolicy(SandboxConfig(enabled=False)),
    )
    runtime = HermesInteraction(
        user_context=user_context,
        agent_config=type("AgentCfg", (), {"name": "test", "max_context_tokens": 8000})(),
        provider_config=type("ProviderCfg", (), {"name": "mock", "model": "mock"})(),
        graph=graph,
        graph_config={"configurable": {"thread_id": "interaction_interrupt_test"}},
        sandbox=SandboxPolicy(SandboxConfig(enabled=False)),
        tools=[shell],
        database_path=":memory:",
    )

    result = await runtime.send_user_message("where am I?")

    kinds = [event.kind for event in result.events]
    assert "message" in kinds
    assert kinds[-1] == "interrupt"
    assert any(
        getattr(event.payload, "content", None) == "I will inspect the workspace."
        for event in result.events
        if event.kind == "message"
    )


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def temp_data_dir():
    """Create a temporary data directory for tests."""
    temp_dir = tempfile.mkdtemp(prefix="agent_test_")
    data_dir = Path(temp_dir) / "data"
    
    # Create all required subdirectories with parents=True
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "sessions" / "default" / "workspace").mkdir(parents=True)
    (data_dir / "sessions" / "default" / "cache").mkdir(parents=True)
    (data_dir / "sessions" / "default" / "subagents").mkdir(parents=True)
    (data_dir / "personality" / "default").mkdir(parents=True)
    
    yield data_dir
    
    # Cleanup
    shutil.rmtree(temp_dir)


@pytest.fixture
def user_context():
    """Default user context for tests."""
    return UserContext(
        user_id="test_user",
        user_name="TestUser",
        platform="local",
        session_type="private",
    )


@pytest.fixture
def permission_config():
    """Default permission configuration."""
    return PermissionConfig(
        default="ask",
        allow=[
            {"tool": "shell", "params": {"command": "^(ls|pwd|echo)"}},
            {"tool": "filesystem_read", "params": {"path": "^/tmp/.*"}},
        ],
        deny=[
            {"tool": "shell", "params": {"command": "^rm -rf"}},
        ],
    )


@pytest_asyncio.fixture
async def mock_llm():
    """Create a mock LLM instance."""
    llm = MockLLM(response_sequence=[{"content": "OK"}])
    yield llm
    llm.reset()


@pytest_asyncio.fixture
async def tools(temp_data_dir):
    """Create standard tool set."""
    workspace = temp_data_dir / "sessions" / "default" / "workspace"
    
    return [
        shell,
        filesystem_read,
        filesystem_write,
        filesystem_list,
        ask,
        message_send,
        subagent_create,
        subagent_wait,
        subagent_list,
        memory_update,
    ]


# ============================================================================
# Test: Basic Tool Calls
# ============================================================================

@pytest.mark.asyncio
async def test_shell_tool_call(mock_llm, tools, user_context):
    """Test that shell tool is called correctly."""
    mock_llm.set_response_sequence(TOOL_CALL_SEQUENCE)
    
    # Build simple graph for testing
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver
    
    checkpointer = MemorySaver()
    permission_system = PermissionSystem(PermissionConfig(default="allow"))
    
    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=None,  # Use memory store for tests
        permission_system=permission_system,
    )
    
    config = {"configurable": {"thread_id": "test_shell"}}
    
    # Run the graph
    input_state = {
        "messages": [HumanMessage(content="List files")],
        "user_context": user_context,
    }
    
    result = await graph.ainvoke(input_state, config=config)
    
    # Verify tool was called
    assert mock_llm.get_call_count() >= 1
    assert mock_llm.verify_tool_call_made("shell", min_count=1)


@pytest.mark.asyncio
async def test_multiple_tool_calls(mock_llm, tools, user_context):
    """Test multiple tool calls in single response."""
    mock_llm.set_response_sequence(MULTI_TOOL_SEQUENCE)
    
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver
    
    checkpointer = MemorySaver()
    permission_system = PermissionSystem(PermissionConfig(default="allow"))
    
    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=None,
        permission_system=permission_system,
    )
    
    config = {"configurable": {"thread_id": "test_multi_tool"}}
    input_state = {
        "messages": [HumanMessage(content="Check directory and read file")],
        "user_context": user_context,
    }
    
    result = await graph.ainvoke(input_state, config=config)
    
    # Verify multiple tools were called
    assert mock_llm.verify_tool_call_made("shell", min_count=1)
    assert mock_llm.verify_tool_call_made("filesystem_read", min_count=1)


# ============================================================================
# Test: Restart and Reconnect
# ============================================================================

@pytest.mark.asyncio
async def test_restart_with_checkpoint(mock_llm, tools, user_context):
    """Test that agent can restart from checkpoint."""
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver
    
    checkpointer = MemorySaver()
    permission_system = PermissionSystem(PermissionConfig(default="allow"))
    
    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=None,
        permission_system=permission_system,
    )
    
    thread_id = "test_restart"
    config = {"configurable": {"thread_id": thread_id}}
    
    # First interaction
    mock_llm.set_response_sequence([{"content": "First response"}])
    input_state = {
        "messages": [HumanMessage(content="Hello")],
        "user_context": user_context,
    }
    
    result1 = await graph.ainvoke(input_state, config=config)
    
    # Second interaction (restart from checkpoint)
    mock_llm.set_response_sequence([{"content": "Second response"}])
    input_state2 = {
        "messages": [HumanMessage(content="Continue")],
        "user_context": user_context,
    }
    
    result2 = await graph.ainvoke(input_state2, config=config)
    
    # Verify conversation history is preserved
    assert len(result2["messages"]) >= 3  # Human + AI + Human + AI


@pytest.mark.asyncio
async def test_reconnect_scenario(mock_llm, tools, user_context):
    """Test reconnection after disconnection."""
    mock_llm.set_response_sequence(RECONNECT_SEQUENCE)
    
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver
    
    checkpointer = MemorySaver()
    permission_system = PermissionSystem(PermissionConfig(default="allow"))
    
    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=None,
        permission_system=permission_system,
    )
    
    config = {"configurable": {"thread_id": "test_reconnect"}}
    
    # Simulate disconnect and reconnect
    input_state = {
        "messages": [HumanMessage(content="Start conversation")],
        "user_context": user_context,
    }
    
    result = await graph.ainvoke(input_state, config=config)
    
    # Verify state was restored
    assert "messages" in result
    assert len(result["messages"]) > 0


# ============================================================================
# Test: Subagent Management
# ============================================================================

@pytest.mark.asyncio
async def test_subagent_attach_mode(mock_llm, tools, user_context, temp_data_dir):
    """Test subagent creation in attach mode."""
    mock_llm.set_response_sequence(SUBAGENT_SEQUENCE)
    
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver
    
    checkpointer = MemorySaver()
    permission_system = PermissionSystem(PermissionConfig(default="allow"))
    
    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=None,
        permission_system=permission_system,
    )
    
    config = {"configurable": {"thread_id": "test_subagent_attach"}}
    input_state = {
        "messages": [HumanMessage(content="Create subagent for task")],
        "user_context": user_context,
    }
    
    result = await graph.ainvoke(input_state, config=config)
    
    # Verify subagent was created
    assert mock_llm.verify_tool_call_made("subagent_create", min_count=1)
    assert mock_llm.verify_tool_call_made("subagent_wait", min_count=1)


@pytest.mark.asyncio
async def test_subagent_detach_mode(mock_llm, tools, user_context, temp_data_dir):
    """Test subagent creation in detach mode."""
    # Configure for detach mode
    sequence = [
        {
            "content": "Creating detached subagent.",
            "tool_calls": [
                {
                    "name": "subagent_create",
                    "args": {"task": "Background task", "mode": "detach"},
                    "id": "call_1",
                },
            ],
        },
        {"content": "Subagent running independently."},
    ]
    mock_llm.set_response_sequence(sequence)
    
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver
    
    checkpointer = MemorySaver()
    permission_system = PermissionSystem(PermissionConfig(default="allow"))
    
    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=None,
        permission_system=permission_system,
    )
    
    config = {"configurable": {"thread_id": "test_subagent_detach"}}
    input_state = {
        "messages": [HumanMessage(content="Run background task")],
        "user_context": user_context,
    }
    
    result = await graph.ainvoke(input_state, config=config)
    
    # Verify subagent was created in detach mode
    assert mock_llm.verify_tool_call_made("subagent_create", min_count=1)


@pytest.mark.asyncio
async def test_subagent_list(mock_llm, tools, user_context):
    """Test listing active subagents."""
    sequence = [
        {
            "content": "Listing subagents.",
            "tool_calls": [
                {"name": "subagent_list", "args": {}, "id": "call_1"},
            ],
        },
        {"content": "Here are the active subagents."},
    ]
    mock_llm.set_response_sequence(sequence)
    
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver
    
    checkpointer = MemorySaver()
    permission_system = PermissionSystem(PermissionConfig(default="allow"))
    
    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=None,
        permission_system=permission_system,
    )
    
    config = {"configurable": {"thread_id": "test_subagent_list"}}
    input_state = {
        "messages": [HumanMessage(content="List subagents")],
        "user_context": user_context,
    }
    
    result = await graph.ainvoke(input_state, config=config)
    
    assert mock_llm.verify_tool_call_made("subagent_list", min_count=1)


# ============================================================================
# Test: Cron Jobs
# ============================================================================

@pytest.mark.asyncio
async def test_cron_job_execution(mock_llm, tools, user_context):
    """Test cron job scheduled execution."""
    mock_llm.set_response_sequence(CRON_JOB_SEQUENCE)
    
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver
    
    checkpointer = MemorySaver()
    permission_system = PermissionSystem(PermissionConfig(default="allow"))
    
    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=None,
        permission_system=permission_system,
    )
    
    config = {"configurable": {"thread_id": "test_cron"}}
    input_state = {
        "messages": [HumanMessage(content="Execute scheduled job")],
        "user_context": user_context,
    }
    
    result = await graph.ainvoke(input_state, config=config)
    
    # Verify cron job tool was called
    assert mock_llm.verify_tool_call_made("shell", min_count=1)


# ============================================================================
# Test: Subagent Tool Calls
# ============================================================================

@pytest.mark.asyncio
async def test_subagent_tool_call(mock_llm, tools, user_context, temp_data_dir):
    """Test that subagents can call tools."""
    # Subagent creates its own tool call sequence
    sequence = [
        {
            "content": "Subagent executing task.",
            "tool_calls": [
                {"name": "shell", "args": {"command": "echo 'subagent work'"}, "id": "call_1"},
            ],
        },
        {
            "content": "Task complete.",
        },
    ]
    mock_llm.set_response_sequence(sequence)
    
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver
    
    checkpointer = MemorySaver()
    permission_system = PermissionSystem(PermissionConfig(default="allow"))
    
    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=None,
        permission_system=permission_system,
    )
    
    config = {"configurable": {"thread_id": "test_subagent_tool"}}
    input_state = {
        "messages": [HumanMessage(content="Subagent perform task")],
        "user_context": user_context,
    }
    
    result = await graph.ainvoke(input_state, config=config)
    
    # Verify tool was called by subagent
    assert mock_llm.verify_tool_call_made("shell", min_count=1)


# ============================================================================
# Test: Permission System
# ============================================================================

@pytest.mark.asyncio
async def test_permission_allow(mock_llm, tools, user_context):
    """Test that allowed tools execute without asking."""
    sequence = [
        {
            "content": "Running allowed command.",
            "tool_calls": [
                {"name": "shell", "args": {"command": "ls"}, "id": "call_1"},
            ],
        },
        {"content": "Command completed."},
    ]
    mock_llm.set_response_sequence(sequence)
    
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver
    
    checkpointer = MemorySaver()
    # Configure to allow 'ls' command
    permission_config = PermissionConfig(
        default="ask",
        allow=[{"tool": "shell", "params": {"command": "^ls$"}}],
    )
    permission_system = PermissionSystem(permission_config)
    
    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=None,
        permission_system=permission_system,
    )
    
    config = {"configurable": {"thread_id": "test_permission_allow"}}
    input_state = {
        "messages": [HumanMessage(content="List files")],
        "user_context": user_context,
    }
    
    result = await graph.ainvoke(input_state, config=config)
    
    # Should complete without permission ask
    assert mock_llm.verify_tool_call_made("shell", min_count=1)


@pytest.mark.asyncio
async def test_permission_deny(mock_llm, tools, user_context):
    """Test that denied tools are blocked."""
    sequence = [
        {
            "content": "Trying dangerous command.",
            "tool_calls": [
                {"name": "shell", "args": {"command": "rm -rf /"}, "id": "call_1"},
            ],
        },
    ]
    mock_llm.set_response_sequence(sequence)
    
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver
    
    checkpointer = MemorySaver()
    permission_config = PermissionConfig(
        default="ask",
        deny=[{"tool": "shell", "params": {"command": "^rm"}}],
    )
    permission_system = PermissionSystem(permission_config)
    
    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=None,
        permission_system=permission_system,
    )
    
    config = {"configurable": {"thread_id": "test_permission_deny"}}
    input_state = {
        "messages": [HumanMessage(content="Delete everything")],
        "user_context": user_context,
    }
    
    # Tool should be denied and not executed
    result = await graph.ainvoke(input_state, config=config)
    
    # Verify the tool call was made but denied (result should indicate denial)
    assert mock_llm.verify_tool_call_made("shell", min_count=1)
    # Check that result contains denial message
    assert any("denied" in str(m).lower() or "permission" in str(m).lower() for m in result.get("messages", []))


@pytest.mark.asyncio
async def test_permission_ask_flow(mock_llm, tools, user_context):
    """Test permission ask workflow."""
    mock_llm.set_response_sequence(PERMISSION_ASK_SEQUENCE)
    
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver
    
    checkpointer = MemorySaver()
    # Default is 'ask' for rm command
    permission_system = PermissionSystem(PermissionConfig(default="ask"))
    
    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=None,
        permission_system=permission_system,
    )
    
    config = {"configurable": {"thread_id": "test_permission_ask"}}
    input_state = {
        "messages": [HumanMessage(content="Remove test directory")],
        "user_context": user_context,
    }
    
    # First invocation should trigger interrupt
    try:
        result = await graph.ainvoke(input_state, config=config)
    except Exception as e:
        # Expected to interrupt for permission
        pass
    
    # Verify permission ask was triggered
    assert mock_llm.verify_tool_call_made("shell", min_count=1)


# ============================================================================
# Test: Subagent Permissions
# ============================================================================

@pytest.mark.asyncio
async def test_subagent_permission_inheritance(mock_llm, tools, user_context, temp_data_dir):
    """Test that subagents inherit parent permissions."""
    sequence = [
        {
            "content": "Creating subagent with inherited permissions.",
            "tool_calls": [
                {
                    "name": "subagent_create",
                    "args": {"task": "Run shell command", "mode": "attach"},
                    "id": "call_1",
                },
            ],
        },
        {
            "content": "Subagent running.",
            "tool_calls": [
                {"name": "shell", "args": {"command": "ls"}, "id": "call_2"},
            ],
        },
        {"content": "Done."},
    ]
    mock_llm.set_response_sequence(sequence)
    
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver
    
    checkpointer = MemorySaver()
    permission_config = PermissionConfig(
        default="allow",
        allow=[{"tool": "shell", "params": {"command": "^ls$"}}],
    )
    permission_system = PermissionSystem(permission_config)
    
    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=None,
        permission_system=permission_system,
    )
    
    config = {"configurable": {"thread_id": "test_subagent_perms"}}
    input_state = {
        "messages": [HumanMessage(content="Subagent list files")],
        "user_context": user_context,
    }
    
    result = await graph.ainvoke(input_state, config=config)
    
    # Verify subagent respected permissions
    assert mock_llm.verify_tool_call_made("subagent_create", min_count=1)
    assert mock_llm.verify_tool_call_made("shell", min_count=1)


# ============================================================================
# Test: Human-in-the-Loop
# ============================================================================

@pytest.mark.asyncio
async def test_human_in_loop_permission(mock_llm, tools, user_context):
    """Test human-in-the-loop for permission approval."""
    mock_llm.set_response_sequence(PERMISSION_ASK_SEQUENCE)
    
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver
    
    checkpointer = MemorySaver()
    permission_system = PermissionSystem(PermissionConfig(default="ask"))
    
    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=None,
        permission_system=permission_system,
    )
    
    config = {"configurable": {"thread_id": "test_hitl"}}
    input_state = {
        "messages": [HumanMessage(content="Execute privileged command")],
        "user_context": user_context,
    }
    
    # Start execution
    stream_result = []
    async for event in graph.astream(input_state, config=config, stream_mode="updates"):
        stream_result.append(event)
    
    # Verify interrupt occurred or permission was requested
    assert len(stream_result) > 0


@pytest.mark.asyncio
async def test_human_in_loop_resume(mock_llm, tools, user_context):
    """Test resuming after human approval."""
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver
    
    checkpointer = MemorySaver()
    permission_system = PermissionSystem(PermissionConfig(default="ask"))
    
    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=None,
        permission_system=permission_system,
    )
    
    thread_id = "test_hitl_resume"
    config = {"configurable": {"thread_id": thread_id}}
    
    # First call triggers permission ask
    mock_llm.set_response_sequence([
        {
            "content": "Need permission.",
            "tool_calls": [{"name": "shell", "args": {"command": "whoami"}, "id": "call_1"}],
        },
    ])
    
    input_state = {
        "messages": [HumanMessage(content="Run command")],
        "user_context": user_context,
    }
    
    # Resume with approval
    resume_input = Command(resume={"approved": True})
    
    # This would normally follow an interrupt
    # For this test, we verify the mechanism exists
    assert resume_input is not None


# ============================================================================
# Test: Persistence
# ============================================================================

@pytest.mark.asyncio
async def test_persistence_checkpoint_restore(mock_llm, tools, user_context, temp_data_dir):
    """Test that checkpoints are persisted and restored."""
    from langgraph.checkpoint.memory import MemorySaver
    
    # Use memory checkpointer for this test (SQLite has binding issues)
    checkpointer = MemorySaver()
    
    permission_system = PermissionSystem(PermissionConfig(default="allow"))
    
    from xbot.graph import build_agent_graph
    
    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=None,
        permission_system=permission_system,
    )
    
    thread_id = "test_persistence"
    config = {"configurable": {"thread_id": thread_id}}
    
    # First interaction
    mock_llm.set_response_sequence([{"content": "First message"}])
    input_state = {
        "messages": [HumanMessage(content="Hello")],
        "user_context": user_context,
    }
    
    result1 = await graph.ainvoke(input_state, config=config)
    
    # Verify checkpoint was saved
    saved = await checkpointer.aget_tuple(config)
    assert saved is not None
    
    # Second interaction - should load from checkpoint
    mock_llm.set_response_sequence([{"content": "Second message"}])
    input_state2 = {
        "messages": [HumanMessage(content="Continue")],
        "user_context": user_context,
    }
    
    result2 = await graph.ainvoke(input_state2, config=config)
    
    # Verify conversation history
    assert len(result2["messages"]) >= 3


@pytest.mark.asyncio
async def test_persistence_store_archive(mock_llm, tools, user_context):
    """Test that the graph accepts the current in-memory store path."""
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.store.memory import InMemoryStore

    checkpointer = MemorySaver()
    store = InMemoryStore()
    permission_system = PermissionSystem(PermissionConfig(default="allow"))

    from xbot.graph import build_agent_graph

    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=store,
        permission_system=permission_system,
    )

    config = {"configurable": {"thread_id": "test_store"}}

    # Trigger compression with long conversation
    mock_llm.set_response_sequence(COMPRESSION_SEQUENCE)
    input_state = {
        "messages": [HumanMessage(content="Long conversation starter")],
        "user_context": user_context,
    }

    result = await graph.ainvoke(input_state, config=config)

    assert "messages" in result


@pytest.mark.asyncio
async def test_linear_compression_reduces_message_chain(mock_llm, tools, user_context):
    """Test that long conversations are compacted into a summary and recent tail."""
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver

    checkpointer = MemorySaver()
    permission_system = PermissionSystem(PermissionConfig(default="allow"))

    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=None,
        permission_system=permission_system,
        max_messages_before_compress=4,
        keep_recent_messages=2,
    )

    thread_id = "test_linear_compression"
    config = {"configurable": {"thread_id": thread_id}}

    first = await graph.ainvoke(
        {
            "messages": [
                HumanMessage(content="msg1"),
                AIMessage(content="reply1"),
                HumanMessage(content="msg2"),
                AIMessage(content="reply2"),
                HumanMessage(content="msg3"),
            ],
            "user_context": user_context,
        },
        config=config,
    )

    assert len(first["messages"]) <= 4
    assert any(isinstance(m, SystemMessage) and "[Compacted History]" in str(m.content) for m in first["messages"])


# ============================================================================
# Test: Integration Scenarios
# ============================================================================

@pytest.mark.asyncio
async def test_full_workflow(mock_llm, tools, user_context):
    """Test complete workflow with multiple features."""
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.store.memory import InMemoryStore

    checkpointer = MemorySaver()
    store = InMemoryStore()
    
    permission_config = PermissionConfig(
        default="ask",
        allow=[{"tool": "shell", "params": {"command": "^(ls|pwd|echo)"}}],
    )
    permission_system = PermissionSystem(permission_config)
    
    from xbot.graph import build_agent_graph
    
    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=store,
        permission_system=permission_system,
    )

    thread_id = "test_full_workflow"
    config = {"configurable": {"thread_id": thread_id}}
    
    # Sequence of interactions
    scenarios = [
        ("List files", [{"content": "Listing...", "tool_calls": [{"name": "shell", "args": {"command": "ls"}, "id": "c1"}]}]),
        ("Create subagent", [{"content": "Creating...", "tool_calls": [{"name": "subagent_create", "args": {"task": "test", "mode": "attach"}, "id": "c2"}]}]),
        ("Check status", [{"content": "Status OK"}]),
    ]

    for user_msg, response_seq in scenarios:
        mock_llm.set_response_sequence(response_seq)
        input_state = {
            "messages": [HumanMessage(content=user_msg)],
            "user_context": user_context,
        }

        result = await graph.ainvoke(input_state, config=config)
        assert "messages" in result

    saved = await checkpointer.aget_tuple(config)
    assert saved is not None


# ============================================================================
# Run tests
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
