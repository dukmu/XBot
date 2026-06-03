"""Tests for tool runtime path resolution and result caching."""

from pathlib import Path

import pytest
from langchain_core.tools import tool as langchain_tool

from xbotv2.core.builtin_tools.filesystem import filesystem_write
from xbotv2.core.engine import Engine
from xbotv2.core.context import ContextBuilder
from xbotv2.hooks.manager import HookManager
from xbotv2.hooks.types import HookStage
from xbotv2.llm.mock import MockLLM
from xbotv2.tools.permissions import PermissionSystem
from xbotv2.tools.registry import ToolRegistry
from xbotv2.tools.result_cache import make_tool_result_cache_hook
from xbotv2.tools.runtime import execute_tools
from xbotv2.tools.sandbox import SandboxPolicy


@langchain_tool
def large_output() -> str:
    """Return a large deterministic string."""
    return "x" * 200


@pytest.mark.asyncio
async def test_sandboxed_tool_paths_resolve_to_workspace(temp_workspace):
    registry = ToolRegistry()
    registry.register(filesystem_write, sandbox_mode="sandboxed")
    sandbox = SandboxPolicy(enabled=False, workspace_root=temp_workspace)

    results = await execute_tools(
        [{"name": "filesystem_write", "args": {"path": "out.txt", "content": "ok"}, "id": "c1"}],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="allow"),
    )

    assert results[0].status == "success"
    assert (temp_workspace / "out.txt").read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
async def test_permission_ask_fails_closed_until_interactive_approval_exists(temp_workspace):
    registry = ToolRegistry()
    registry.register(filesystem_write, sandbox_mode="sandboxed")
    sandbox = SandboxPolicy(enabled=False, workspace_root=temp_workspace)

    results = await execute_tools(
        [{"name": "filesystem_write", "args": {"path": "blocked.txt", "content": "no"}, "id": "c1"}],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="ask"),
    )

    assert results[0].status == "error"
    assert "Interactive approval is not implemented" in results[0].content
    assert not (temp_workspace / "blocked.txt").exists()


@pytest.mark.asyncio
async def test_after_tools_cache_hook_truncates_before_history_and_events(state_store, temp_workspace):
    registry = ToolRegistry()
    registry.register(large_output, sandbox_mode="host")
    hook_manager = HookManager()
    hook_manager.register(
        HookStage.AFTER_TOOLS,
        make_tool_result_cache_hook(
            state_store,
            max_inline_chars=100,
            preview_chars=20,
        ),
    )
    llm = MockLLM(responses=[
        {
            "content": "calling",
            "tool_calls": [{"name": "large_output", "args": {}, "id": "call_large"}],
        },
        {"content": "done"},
    ])
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

    events = [e async for e in engine.run_turn("run large")]
    tool_event = next(e for e in events if e["type"] == "tool_result")
    tool_message = next(m for m in engine.messages if type(m).__name__ == "ToolMessage")

    assert "[Tool result cached]" in tool_event["data"]["content"]
    assert "[Tool result cached]" in tool_message.content
    assert "x" * 100 not in tool_message.content

    cache_files = list((Path(state_store.artifacts_dir) / "tool_results").glob("*.txt"))
    assert len(cache_files) == 1
    assert cache_files[0].read_text(encoding="utf-8") == "x" * 200

    event_types = [event["type"] for event in state_store.read_events()]
    assert "tool_result_cached" in event_types
