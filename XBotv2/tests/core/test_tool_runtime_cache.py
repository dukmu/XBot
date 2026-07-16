"""Tests for tool runtime path resolution and result caching."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.tools import tool as langchain_tool

from xbotv2.core.builtin_tools.filesystem import (
    filesystem_find,
    filesystem_list,
    filesystem_read,
    filesystem_search,
    filesystem_write,
)
from xbotv2.core.builtin_tools.interaction import ask_user
from xbotv2.core.engine import Engine
from xbotv2.core.context import ContextBuilder
from xbotv2.hooks.manager import HookManager
from xbotv2.api.hooks import HookContext, HookStage, SessionInfo
from xbotv2.api.messages import Message
from xbotv2.llm.mock import MockLLM
from xbotv2.tools.permissions import PermissionSystem
from xbotv2.tools.registry import ToolRegistry
from xbotv2.tools.result_cache import make_tool_result_cache_hook
from xbotv2.tools.runtime import execute_tools
from xbotv2.tools.sandbox import SandboxPolicy
from xbotv2.api.tools import Tool, ToolCall


def large_output() -> str:
    """Return a large deterministic string."""
    return "x" * 200
large_output_tool = Tool.from_function(large_output, name="large_output")


def failing_tool() -> str:
    """Raise a deterministic tool failure."""
    raise RuntimeError("boom")
failing_tool_tool = Tool.from_function(failing_tool, name="failing_tool")


@pytest.mark.asyncio
async def test_sync_langchain_tool_uses_invoke_not_ainvoke(monkeypatch):
    def sync_tool(message: str) -> str:
        return f"ok:{message}"

    xbot_tool = Tool.from_function(sync_tool, name="sync_tool")
    registry = ToolRegistry()
    registry.register(xbot_tool, sandbox_mode="host")

    results = await execute_tools(
        [ToolCall("c1", "sync_tool", {"message": "hello"})],
        registry,
        permission_system=PermissionSystem(default_decision="allow"),
    )

    assert results[0].status == "success"
    assert results[0].content == "ok:hello"


@pytest.mark.asyncio
async def test_sandboxed_tool_paths_resolve_to_workspace(temp_workspace):
    registry = ToolRegistry()
    registry.register(filesystem_write, sandbox_mode="sandboxed")
    sandbox = SandboxPolicy(enabled=False, workspace_root=temp_workspace)

    results = await execute_tools(
        [
            ToolCall(
                "c1",
                "filesystem_write",
                {"path": str(temp_workspace / "out.txt"), "content": "ok"},
            )
        ],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="allow"),
    )

    assert results[0].status == "success"
    assert (temp_workspace / "out.txt").read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
async def test_cached_result_path_resolves_from_session_state_when_sandbox_disabled(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "data" / "sessions" / "s" / "state"
    cached = session_root / "artifacts" / "tool_results" / "cached.txt"
    workspace.mkdir()
    cached.parent.mkdir(parents=True)
    cached.write_text("cached content", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(filesystem_read, sandbox_mode="sandboxed")
    sandbox = SandboxPolicy(
        enabled=False,
        data_root=tmp_path / "data",
        workspace_root=workspace,
        session_root=session_root,
    )

    results = await execute_tools(
        [ToolCall("c1", "filesystem_read", {
            "path": "session/artifacts/tool_results/cached.txt",
        })],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="allow"),
    )

    assert results[0].status == "success"
    assert "cached content" in results[0].content


@pytest.mark.asyncio
async def test_session_namespace_supports_read_only_discovery_when_sandbox_disabled(tmp_path):
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "data" / "sessions" / "s" / "state"
    cached = session_root / "artifacts" / "tool_results" / "cached.txt"
    workspace.mkdir()
    cached.parent.mkdir(parents=True)
    cached.write_text("cached content", encoding="utf-8")
    sandbox = SandboxPolicy(
        enabled=False,
        workspace_root=workspace,
        session_root=session_root,
    )
    registry = ToolRegistry()
    for tool in (filesystem_list, filesystem_search, filesystem_find):
        registry.register(tool, sandbox_mode="sandboxed")

    results = await execute_tools(
        [
            ToolCall("list", "filesystem_list", {"path": "session/artifacts"}),
            ToolCall("search", "search_text", {
                "path": "session/artifacts", "pattern": "cached",
            }),
            ToolCall("find", "find_files", {
                "path": "session/artifacts", "pattern": "*.txt",
            }),
        ],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="allow"),
    )

    assert all(result.status == "success" for result in results)
    assert "tool_results" in results[0].content
    assert "tool_results/cached.txt:1:cached content" in results[1].content
    assert "tool_results/cached.txt" in results[2].content


@pytest.mark.asyncio
async def test_host_tool_does_not_receive_enabled_sandbox(temp_workspace):
    seen = []

    async def inspect_backend(*, sandbox=None):
        seen.append(sandbox)
        return "ok"

    registry = ToolRegistry()
    registry.register(Tool.from_function(inspect_backend), sandbox_mode="host")
    sandbox = SandboxPolicy(enabled=True, workspace_root=temp_workspace)

    results = await execute_tools(
        [ToolCall("c1", "inspect_backend", {})],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="allow"),
    )

    assert results[0].status == "success"
    assert seen == [None]


@pytest.mark.asyncio
async def test_sandboxed_tool_receives_enabled_sandbox(temp_workspace):
    seen = []

    async def inspect_backend(*, sandbox=None):
        seen.append(sandbox)
        return "ok"

    registry = ToolRegistry()
    registry.register(Tool.from_function(inspect_backend), sandbox_mode="sandboxed")
    sandbox = SandboxPolicy(enabled=True, workspace_root=temp_workspace)

    results = await execute_tools(
        [ToolCall("c1", "inspect_backend", {})],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="allow"),
    )

    assert results[0].status == "success"
    assert seen == [sandbox]


@pytest.mark.asyncio
async def test_permission_ask_fails_closed_until_tool_replay_exists(temp_workspace):
    registry = ToolRegistry()
    registry.register(filesystem_write, sandbox_mode="host")
    sandbox = SandboxPolicy(enabled=False, workspace_root=temp_workspace)

    results = await execute_tools(
        [ToolCall("c1", "filesystem_write", {"path": "blocked.txt", "content": "no"})],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="ask"),
    )

    assert results[0].status == "error"
    assert "No live permission handler is available" in results[0].content
    assert "fails closed" in results[0].content
    assert not (temp_workspace / "blocked.txt").exists()


@pytest.mark.asyncio
async def test_live_permission_allow_executes_current_tool_call(temp_workspace):
    registry = ToolRegistry()
    registry.register(filesystem_write, sandbox_mode="host")
    sandbox = SandboxPolicy(enabled=False, workspace_root=temp_workspace)
    seen = []

    async def approve(event, **kwargs):
        seen.append((event["type"], event["data"]["request_id"], kwargs["tool_call_id"]))
        return {
            "request_id": event["data"]["request_id"],
            "status": "answered",
            "decision": "allow",
        }

    results = await execute_tools(
        [
            ToolCall(
                "c1",
                "filesystem_write",
                {"path": str(temp_workspace / "allowed.txt"), "content": "ok"},
            )
        ],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="ask"),
        permission_interaction_handler=approve,
    )

    assert seen == [("permission_request", "permission:c1", "c1")]
    assert results[0].status == "success"
    assert (temp_workspace / "allowed.txt").read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
async def test_builtin_ask_user_waits_for_live_answer() -> None:
    registry = ToolRegistry()
    registry.register(ask_user, sandbox_mode="host")
    seen: list[tuple[str, str, float | None]] = []

    async def answer(event, **kwargs):
        seen.append((
            event["type"],
            event["data"]["request_id"],
            kwargs["timeout_seconds"],
        ))
        return {
            "request_id": event["data"]["request_id"],
            "status": "answered",
            "answer": "continue",
        }

    results = await execute_tools(
        [ToolCall(
            "c1",
            "ask_user",
            {
                "question": "Continue?",
                "options": [
                    {"label": "continue", "description": "Keep running."},
                    {"label": "stop", "description": "Stop the current work."},
                ],
                "timeout_seconds": 3,
            },
        )],
        registry,
        permission_system=PermissionSystem(default_decision="allow"),
        client_interaction_handler=answer,
    )

    assert seen == [("user_input_required", "user_input:c1", 3)]
    assert results[0].status == "success"
    assert results[0].content == "User answered: continue"


@pytest.mark.asyncio
async def test_builtin_ask_user_rejects_empty_or_unstructured_options() -> None:
    registry = ToolRegistry()
    registry.register(ask_user, sandbox_mode="host")
    called = False

    async def answer(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"status": "answered", "answer": "unused"}

    results = await execute_tools(
        [
            ToolCall("c1", "ask_user", {
                "question": "Continue?",
                "options": [
                    {"label": "", "description": "Continue."},
                    {"label": "stop", "description": "Stop."},
                ],
            }),
            ToolCall("c2", "ask_user", {
                "question": "Continue?",
                "options": [
                    {"content": "continue"},
                    {"content": "stop"},
                ],
            }),
        ],
        registry,
        permission_system=PermissionSystem(default_decision="allow"),
        client_interaction_handler=answer,
    )

    assert called is False
    assert [result.status for result in results] == ["error", "error"]
    assert "Invalid arguments for ask_user" in results[0].content
    assert "Invalid arguments for ask_user" in results[1].content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_status"),
    [
        ({"status": "answered", "answer": ""}, "error"),
        ({"status": "timeout"}, "error"),
        ({"status": "cancelled", "reason": "interrupted"}, "cancelled"),
    ],
)
async def test_builtin_ask_user_preserves_unsuccessful_outcomes(
    response,
    expected_status,
) -> None:
    registry = ToolRegistry()
    registry.register(ask_user, sandbox_mode="host")

    async def answer(*_args, **_kwargs):
        return response

    results = await execute_tools(
        [ToolCall("c1", "ask_user", {"question": "Continue?"})],
        registry,
        permission_system=PermissionSystem(default_decision="allow"),
        client_interaction_handler=answer,
    )

    assert results[0].status == expected_status


@pytest.mark.asyncio
async def test_dictionary_tool_result_preserves_structured_fields() -> None:
    def structured_result() -> dict:
        return {
            "status": "error",
            "content": "failed",
            "data": {"attempt": 1},
            "error": {
                "code": "dict_error",
                "message": "failed",
                "retryable": False,
                "details": {},
            },
            "artifacts": [{
                "id": "artifact-1",
                "media_type": "text/plain",
                "name": "result.txt",
            }],
        }

    registry = ToolRegistry()
    registry.register(
        Tool.from_function(structured_result),
        sandbox_mode="host",
    )

    results = await execute_tools(
        [ToolCall("c1", "structured_result", {})],
        registry,
        permission_system=PermissionSystem(default_decision="allow"),
    )

    assert results[0].additional_kwargs == {
        "xbotv2_data": {"attempt": 1},
        "xbotv2_error": {
            "code": "dict_error",
            "message": "failed",
            "retryable": False,
            "details": {},
        },
    }
    assert results[0].artifact == [{
        "id": "artifact-1",
        "media_type": "text/plain",
        "name": "result.txt",
    }]


@pytest.mark.asyncio
async def test_permission_and_batch_hooks_fire(temp_workspace):
    registry = ToolRegistry()
    registry.register(filesystem_write, sandbox_mode="sandboxed")
    sandbox = SandboxPolicy(enabled=False, workspace_root=temp_workspace)
    hook_manager = HookManager()
    calls = []

    async def permission_request(ctx):
        calls.append(("permission_request", ctx.tool_call.name, ctx.permission_decision))

    async def tool_denied(ctx):
        calls.append(("denied", ctx.tool_call.name, type(ctx.error).__name__))

    async def post_batch(ctx):
        calls.append(("batch", len(ctx.tool_calls), len(ctx.tool_results)))

    hook_manager.register(HookStage.ON_PERMISSION_REQUEST, permission_request)
    hook_manager.register(HookStage.ON_TOOL_DENIED, tool_denied)
    hook_manager.register(HookStage.POST_TOOL_BATCH, post_batch)

    results = await execute_tools(
        [ToolCall("c1", "filesystem_write", {"path": "blocked.txt", "content": "no"})],
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
    registry.register(failing_tool_tool, sandbox_mode="host")
    hook_manager = HookManager()
    calls = []

    async def failure(ctx):
        calls.append((ctx.tool_call.name, type(ctx.error).__name__, ctx.tool_result.status))

    hook_manager.register(HookStage.ON_TOOL_CALL_FAILURE, failure)

    results = await execute_tools(
        [ToolCall("c1", "failing_tool", {})],
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
    registry.register(filesystem_write, sandbox_mode="host")
    sandbox = SandboxPolicy(enabled=False, workspace_root=temp_workspace)
    hook_manager = HookManager()
    calls = []

    async def rewrite_tool_call(ctx):
        calls.append(("before", ctx.tool_call.id, ctx.tool_call.args["path"]))
        return {
            "tool_call": ToolCall(
                "rewritten_id",
                ctx.tool_call.name,
                {
                    "path": str(temp_workspace / "rewritten.txt"),
                    "content": "ok",
                },
            )
        }

    async def after_tool_call(ctx):
        calls.append((
            "after",
            ctx.tool_call.id,
            ctx.tool_call.args["path"],
            ctx.tool_result.tool_call_id,
        ))

    async def post_batch(ctx):
        calls.append((
            "batch",
            ctx.tool_calls[0].id,
            ctx.tool_calls[0].args["path"],
            ctx.tool_results[0].tool_call_id,
        ))

    hook_manager.register(HookStage.BEFORE_TOOL_CALL, rewrite_tool_call)
    hook_manager.register(HookStage.AFTER_TOOL_CALL, after_tool_call)
    hook_manager.register(HookStage.POST_TOOL_BATCH, post_batch)

    results = await execute_tools(
        [
            ToolCall(
                "old_id",
                "filesystem_write",
                {"path": "old.txt", "content": "no"},
            )
        ],
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
    assert calls[0] == ("before", "old_id", "old.txt")
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
    registry.register(large_output_tool, sandbox_mode="host")
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
    tool_message = next(m for m in engine.messages if m.role == "tool")

    assert "[Tool result cached]" in tool_event["data"]["content"]
    assert "[Tool result cached]" in tool_message.content
    assert "x" * 100 not in tool_message.content
    assert "Ending excerpt" in tool_message.content
    assert "filesystem_read using offset and limit" in tool_message.content
    assert tool_message.artifact["kind"] == "cached_tool_result"
    assert tool_message.artifact["tool_call_id"] == "call_large"
    assert tool_message.artifact["cache_path"].startswith("session/artifacts/tool_results/")
    assert not Path(tool_message.artifact["cache_path"]).is_absolute()
    assert str(state_store.root) not in tool_message.content

    cache_files = list((Path(state_store.artifacts_dir) / "tool_results").glob("*.txt"))
    assert len(cache_files) == 1
    assert cache_files[0].read_text(encoding="utf-8") == "x" * 200

    restored_tool_message = next(
        m for m in state_store.read_messages() if m.role == "tool"
    )
    assert restored_tool_message.artifact == tool_message.artifact


@pytest.mark.asyncio
async def test_cache_hook_externalizes_large_structured_data(state_store):
    hook = make_tool_result_cache_hook(
        state_store,
        max_inline_chars=100,
        preview_chars=20,
    )
    message = Message(
        role="tool",
        content="Short result summary.",
        tool_call_id="large-data",
        additional_kwargs={"xbotv2_data": {"content": "x" * 200}},
    )
    ctx = SimpleNamespace(tool_results=[message])

    await hook(ctx)

    data = message.additional_kwargs["xbotv2_data"]
    assert data["cached"] is True
    assert data["cache_path"].startswith("session/artifacts/tool_results/")
    assert not Path(data["cache_path"]).is_absolute()
    assert message.artifact["data_cache_path"] == data["cache_path"]
    assert message.artifact["kind"] == "cached_tool_data"
    assert message.content == "Short result summary."


def _hook_context(stage, **kwargs):
    return HookContext(
        stage=stage,
        session=SessionInfo(session_id="s", thread_id="t", workspace_root="/workspace", provider="p"),
        **kwargs,
    )
