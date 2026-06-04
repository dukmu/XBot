"""Tests for tool runtime path resolution and result caching."""

from pathlib import Path

import pytest
from langchain_core.tools import tool as langchain_tool

from xbotv2.core.builtin_tools.filesystem import filesystem_write
from xbotv2.core.engine import Engine
from xbotv2.core.context import ContextBuilder
from xbotv2.hooks.manager import HookManager
from xbotv2.hooks.types import HookContext, HookStage, SessionInfo
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


@langchain_tool
def failing_tool() -> str:
    """Raise a deterministic tool failure."""
    raise RuntimeError("boom")


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
async def test_permission_ask_fails_closed_until_tool_replay_exists(temp_workspace):
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
    assert "permission.response can record the decision" in results[0].content
    assert "fails closed and is not replayed" in results[0].content
    assert not (temp_workspace / "blocked.txt").exists()


@pytest.mark.asyncio
async def test_permission_and_batch_hooks_fire(temp_workspace):
    registry = ToolRegistry()
    registry.register(filesystem_write, sandbox_mode="sandboxed")
    sandbox = SandboxPolicy(enabled=False, workspace_root=temp_workspace)
    hook_manager = HookManager()
    calls = []

    async def permission_request(ctx):
        calls.append(("permission_request", ctx.tool_call["name"], ctx.permission_decision))

    async def tool_denied(ctx):
        calls.append(("denied", ctx.tool_call["name"], type(ctx.error).__name__))

    async def post_batch(ctx):
        calls.append(("batch", len(ctx.tool_calls), len(ctx.tool_results)))

    hook_manager.register(HookStage.ON_PERMISSION_REQUEST, permission_request)
    hook_manager.register(HookStage.ON_TOOL_DENIED, tool_denied)
    hook_manager.register(HookStage.POST_TOOL_BATCH, post_batch)

    results = await execute_tools(
        [{"name": "filesystem_write", "args": {"path": "blocked.txt", "content": "no"}, "id": "c1"}],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="ask"),
        hook_manager=hook_manager,
        hook_context_factory=_hook_context,
    )

    assert results[0].status == "error"
    assert calls == [
        ("permission_request", "filesystem_write", "ask"),
        ("denied", "filesystem_write", "PermissionError"),
        ("batch", 1, 1),
    ]


@pytest.mark.asyncio
async def test_tool_failure_hook_fires(temp_workspace):
    registry = ToolRegistry()
    registry.register(failing_tool, sandbox_mode="host")
    hook_manager = HookManager()
    calls = []

    async def failure(ctx):
        calls.append((ctx.tool_call["name"], type(ctx.error).__name__, ctx.tool_result.status))

    hook_manager.register(HookStage.ON_TOOL_CALL_FAILURE, failure)

    results = await execute_tools(
        [{"name": "failing_tool", "args": {}, "id": "c1"}],
        registry,
        permission_system=PermissionSystem(default_decision="allow"),
        hook_manager=hook_manager,
        hook_context_factory=_hook_context,
    )

    assert results[0].status == "error"
    assert calls == [("failing_tool", "RuntimeError", "error")]


@pytest.mark.asyncio
async def test_before_tool_call_rewrite_updates_tool_id_and_resolves_paths(temp_workspace):
    registry = ToolRegistry()
    registry.register(filesystem_write, sandbox_mode="sandboxed")
    sandbox = SandboxPolicy(enabled=False, workspace_root=temp_workspace)
    hook_manager = HookManager()
    calls = []

    async def rewrite_tool_call(ctx):
        calls.append(("before", ctx.tool_call["id"], ctx.tool_call["args"]["path"]))
        return {
            "tool_call": {
                "id": "rewritten_id",
                "args": {"path": "rewritten.txt", "content": "ok"},
            }
        }

    async def after_tool_call(ctx):
        calls.append((
            "after",
            ctx.tool_call["id"],
            ctx.tool_call["args"]["path"],
            ctx.tool_result.tool_call_id,
        ))

    async def post_batch(ctx):
        calls.append((
            "batch",
            ctx.tool_calls[0]["id"],
            ctx.tool_calls[0]["args"]["path"],
            ctx.tool_results[0].tool_call_id,
        ))

    hook_manager.register(HookStage.BEFORE_TOOL_CALL, rewrite_tool_call)
    hook_manager.register(HookStage.AFTER_TOOL_CALL, after_tool_call)
    hook_manager.register(HookStage.POST_TOOL_BATCH, post_batch)

    results = await execute_tools(
        [{"name": "filesystem_write", "args": {"path": "old.txt", "content": "no"}, "id": "old_id"}],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="allow"),
        hook_manager=hook_manager,
        hook_context_factory=_hook_context,
    )

    assert results[0].status == "success"
    assert results[0].tool_call_id == "rewritten_id"
    assert not (temp_workspace / "old.txt").exists()
    assert (temp_workspace / "rewritten.txt").read_text(encoding="utf-8") == "ok"
    assert calls[0] == ("before", "old_id", str(temp_workspace / "old.txt"))
    assert calls[1] == (
        "after",
        "rewritten_id",
        str(temp_workspace / "rewritten.txt"),
        "rewritten_id",
    )
    assert calls[2] == (
        "batch",
        "rewritten_id",
        str(temp_workspace / "rewritten.txt"),
        "rewritten_id",
    )


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
    assert tool_message.artifact["kind"] == "cached_tool_result"
    assert tool_message.artifact["tool_call_id"] == "call_large"

    cache_files = list((Path(state_store.artifacts_dir) / "tool_results").glob("*.txt"))
    assert len(cache_files) == 1
    assert cache_files[0].read_text(encoding="utf-8") == "x" * 200

    cache_event = next(event for event in state_store.read_events() if event["type"] == "tool_result_cached")
    assert cache_event["payload"]["cache_path"] == str(cache_files[0])
    assert cache_event["payload"]["sha256"] == tool_message.artifact["sha256"]

    restored_tool_message = next(
        m for m in state_store.read_messages() if type(m).__name__ == "ToolMessage"
    )
    assert restored_tool_message.artifact == tool_message.artifact


def _hook_context(stage, **kwargs):
    return HookContext(
        stage=stage,
        session=SessionInfo(session_id="s", thread_id="t", personality_id="p"),
        **kwargs,
    )
