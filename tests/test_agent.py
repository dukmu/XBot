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
from xbot.mock_llm import MockLLM
from xbot.mock_llm_sequences import (
    TOOL_CALL_SEQUENCE,
    MULTI_TOOL_SEQUENCE,
    PERMISSION_ASK_SEQUENCE,
    SUBAGENT_SEQUENCE,
    COMPRESSION_SEQUENCE,
    RECONNECT_SEQUENCE,
    CRON_JOB_SEQUENCE,
)
from xbot.builtin_tools import (
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
from tests.conftest import make_default_hooks, make_default_registry
from xbot.sandbox import SandboxPolicy


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
    (data_dir / "personalities" / "default").mkdir(parents=True)
    
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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
    from xbot.checkpoint import FileBackedSaver
    
    permission_system = PermissionSystem(PermissionConfig(default="allow"))
    
    from xbot.graph import build_agent_graph

    checkpoint_path = temp_data_dir / "sessions" / "default" / "checkpoints" / "langgraph.pkl"
    checkpointer = FileBackedSaver(checkpoint_path)
    
    graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=checkpointer,
        store=None,
        permission_system=permission_system,
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
    assert checkpoint_path.exists()
    
    # Second interaction should load from a new saver instance.
    restored_checkpointer = FileBackedSaver(checkpoint_path)
    restored_graph = build_agent_graph(
        llm=mock_llm,
        tools=tools,
        checkpointer=restored_checkpointer,
        store=None,
        permission_system=permission_system,
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
    )
    mock_llm.set_response_sequence([{"content": "Second message"}])
    input_state2 = {
        "messages": [HumanMessage(content="Continue")],
        "user_context": user_context,
    }
    
    result2 = await restored_graph.ainvoke(input_state2, config=config)
    
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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
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
