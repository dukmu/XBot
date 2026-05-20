"""Focused runtime boundary tests for Hermes."""

import shutil

import pytest

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import tool as lc_tool
from langgraph.checkpoint.memory import MemorySaver

from xbot.models import PermissionConfig, SandboxConfig
from xbot.permissions import PermissionSystem
from xbot.sandbox import SandboxPolicy
from xbot.tools import compact, filesystem_read, filesystem_write, shell

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
async def test_interaction_batch_does_not_persist_compaction_event(mock_llm, user_context):
    """Runtime events should not be stored in graph state for later replay."""
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

    assert status_events == []


@pytest.mark.asyncio
async def test_prepare_context_ignores_stale_runtime_events(mock_llm):
    """Runtime events are custom stream events, not durable state."""
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

    assert result == {}


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


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is required")
@pytest.mark.asyncio
async def test_tool_confirm_combines_permission_and_sandbox_asks(mock_llm, user_context, temp_data_dir):
    """A tool needing both approvals should ask the user once."""
    from xbot.interaction import HermesInteraction
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver

    workspace = temp_data_dir / "sessions" / "default" / "workspace"
    ask_dir = workspace / "approval"
    ask_dir.mkdir(parents=True)
    target = ask_dir / "note.txt"
    mock_llm.set_response_sequence([
        {
            "content": "writing",
            "tool_calls": [
                {"name": "filesystem_write", "args": {"path": str(target), "content": "ok"}, "id": "call_1"},
            ],
        },
    ])
    sandbox_policy = SandboxPolicy(
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
    graph = build_agent_graph(
        llm=mock_llm,
        tools=[filesystem_write],
        checkpointer=MemorySaver(),
        store=None,
        permission_system=PermissionSystem(PermissionConfig(default="ask")),
        sandbox_policy=sandbox_policy,
    )
    runtime = HermesInteraction(
        user_context=user_context,
        agent_config=type("AgentCfg", (), {"name": "test", "max_context_tokens": 8000})(),
        provider_config=type("ProviderCfg", (), {"name": "mock", "model": "mock"})(),
        graph=graph,
        graph_config={"configurable": {"thread_id": "combined_tool_confirm_test"}},
        sandbox=sandbox_policy,
        tools=[filesystem_write],
        database_path=":memory:",
    )

    result = await runtime.send_user_message("write")
    interrupts = [event for event in result.events if event.kind == "interrupt"]

    assert len(interrupts) == 1
    payload = interrupts[0].payload
    assert payload["type"] == "tool_confirm"
    assert payload["permission"]
    assert payload["sandbox"]["path"] == str(target)
    assert len(payload["reasons"]) == 2

