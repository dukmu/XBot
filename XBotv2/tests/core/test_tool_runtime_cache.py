"""Tests for tool runtime path resolution and result caching."""

import json
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from XBotv2.coretools.filesystem import (
    filesystem_copy,
    filesystem_find,
    filesystem_list,
    filesystem_read,
    filesystem_search,
    filesystem_write,
)
from XBotv2.coretools.interaction import ask_user
from XBotv2.agentloop.engine import Engine
from XBotv2.context_builder.builder import ContextBuilder
from XBotv2.config.models import RuntimeConfig
import xcore
from XBotv2.core.events import EventContext, Events
from XBotv2.core.runtime import SessionInfo
from XBotv2.core.messages import Message
from XBotv2.protocol.models import PermissionRequestData
from XBotv2.llm.mock import MockLLM
from XBotv2.permissions.system import PermissionSystem
from XBotv2.tools.registry import ToolRegistry
from XBotv2.coretools.result_cache import make_tool_result_cache_hook
from XBotv2.tools.runtime import execute_tools
from XBotv2.sandbox.policy import SandboxPolicy
from XBotv2.core.tools import ArtifactRef, Tool, ToolCall, ToolError, ToolResult


async def large_output() -> str:
    """Return a large deterministic string."""
    return "x" * 200


large_output_tool = Tool.from_function(large_output, name="large_output")


async def failing_tool() -> str:
    """Raise a deterministic tool failure."""
    raise RuntimeError("boom")


failing_tool_tool = Tool.from_function(failing_tool, name="failing_tool")


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
    assert "tool_results/cached.txt:1:1:cached content" in results[1].content
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
async def test_tool_result_preserves_structured_fields() -> None:
    async def structured_result() -> ToolResult:
        return ToolResult(
            status="error",
            content="failed",
            data={"attempt": 1},
            error=ToolError("dict_error", "failed"),
            artifacts=(ArtifactRef("artifact-1", "text/plain", "result.txt"),),
        )

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

    assert results[0].status == "error"
    assert results[0].data == {"attempt": 1}
    assert results[0].error["code"] == "dict_error"
    assert results[0].artifact[0].name == "result.txt"


@pytest.mark.asyncio
async def test_plain_dictionary_result_is_model_content() -> None:
    async def plain_result() -> dict:
        return {"data": {"value": 1}}

    registry = ToolRegistry()
    registry.register(Tool.from_function(plain_result), sandbox_mode="host")

    results = await execute_tools(
        [ToolCall("c1", "plain_result", {})],
        registry,
        permission_system=PermissionSystem(default_decision="allow"),
    )

    assert results[0].status == "success"
    assert json.loads(results[0].content) == {"data": {"value": 1}}
    assert results[0].data is None


@pytest.mark.asyncio
async def test_permission_and_batch_hooks_fire(temp_workspace):
    registry = ToolRegistry()
    registry.register(filesystem_write, sandbox_mode="sandboxed")
    sandbox = SandboxPolicy(enabled=False, workspace_root=temp_workspace)
    plugin_ctx = xcore.Context()
    calls = []

    async def permission_request(ctx):
        calls.append(("permission_request", ctx.tool_call.name, ctx.permission_decision))

    async def tool_denied(ctx):
        calls.append(("denied", ctx.tool_call.name, type(ctx.error).__name__))

    async def post_batch(ctx):
        calls.append(("batch", len(ctx.tool_calls), len(ctx.tool_results)))

    plugin_ctx.on(Events.PERMISSION_REQUEST, permission_request)
    plugin_ctx.on(Events.TOOL_DENIED, tool_denied)
    plugin_ctx.on(Events.POST_TOOL_BATCH, post_batch)

    results = await execute_tools(
        [ToolCall("c1", "filesystem_write", {"path": "blocked.txt", "content": "no"})],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="ask"),
        plugin_ctx=plugin_ctx,
        context_factory=_event_context,
    )

    assert results[0].status == "error"
    assert calls == [
        ("permission_request", "filesystem_write", "ask"),
        ("denied", "filesystem_write", "PermissionError"),
        ("batch", 1, 1),
    ]


@pytest.mark.asyncio
async def test_sandbox_path_approval_records_exact_external_read(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("approved\n", encoding="utf-8")
    sandbox = SandboxPolicy(
        config={"external_read": "ask", "external_write": "deny"},
        workspace_root=workspace,
    )
    if not sandbox.backend_available:
        pytest.skip("bubblewrap is not installed")
    registry = ToolRegistry()
    registry.register(filesystem_read, sandbox_mode="sandboxed")
    events = []

    async def approve(event, **_kwargs):
        events.append(event)
        return {"status": "answered", "decision": "allow", "scope": "once"}

    results = await execute_tools(
        [ToolCall("c1", "filesystem_read", {"path": str(external)})],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="allow"),
        permission_interaction_handler=approve,
    )

    assert results[0].status == "success"
    assert results[0].content == "approved\n"
    assert events[0]["data"]["source"] == "sandbox"
    assert "sandbox_path" not in events[0]["data"]
    assert "sandbox_access" not in events[0]["data"]
    PermissionRequestData.model_validate(events[0]["data"])

    await execute_tools(
        [ToolCall("c2", "filesystem_read", {"path": str(external)})],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="allow"),
        permission_interaction_handler=approve,
    )
    assert len(events) == 2


@pytest.mark.asyncio
async def test_shell_can_request_sandbox_escalation_before_execution(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = SandboxPolicy(
        config={"external_write": "ask"},
        workspace_root=workspace,
    )
    calls = []

    async def shell(
        sandbox_permissions: str,
        justification: str,
        cwd: str | None = None,
    ) -> str:
        del cwd
        calls.append((sandbox_permissions, justification))
        return "ran"

    registry = ToolRegistry()
    registry.register(
        Tool.from_function(shell, name="shell"),
        sandbox_mode="sandboxed",
    )
    events = []

    async def approve(event, **kwargs):
        events.append((event, kwargs))
        return {"status": "answered", "decision": "allow", "scope": "once"}

    results = await execute_tools(
        [ToolCall("c1", "shell", {
            "sandbox_permissions": "require_escalated",
            "justification": "Install a required dependency.",
        })],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="allow"),
        permission_interaction_handler=approve,
    )

    assert results[0].status == "success"
    assert calls == [(
        "require_escalated",
        "Install a required dependency.",
    )]
    event, _ = events[0]
    assert event["data"]["source"] == "sandbox"
    assert "Install a required dependency." in event["data"]["reason"]


@pytest.mark.asyncio
async def test_sandbox_copy_checks_both_paths_with_one_approval(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("copy me\n", encoding="utf-8")
    sandbox = SandboxPolicy(
        config={"external_read": "ask", "external_write": "ask"},
        workspace_root=workspace,
    )
    if not sandbox.backend_available:
        pytest.skip("bubblewrap is not installed")
    registry = ToolRegistry()
    registry.register(filesystem_copy, sandbox_mode="sandboxed")
    events = []

    async def approve(event, **_kwargs):
        events.append(event)
        return {"status": "answered", "decision": "allow", "scope": "once"}

    results = await execute_tools(
        [ToolCall(
            "c1",
            "filesystem_copy",
            {
                "source": str(source),
                "destination": str(destination),
            },
        )],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="allow"),
        permission_interaction_handler=approve,
    )

    assert results[0].status == "success"
    assert destination.read_text(encoding="utf-8") == "copy me\n"
    assert len(events) == 1
    assert str(source) in events[0]["data"]["reason"]
    assert str(destination) in events[0]["data"]["reason"]


@pytest.mark.asyncio
async def test_sandbox_workspace_write_deny_fails_before_mutation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = SandboxPolicy(
        config={"workspace_read": "allow", "workspace_write": "deny"},
        workspace_root=workspace,
    )
    registry = ToolRegistry()
    registry.register(filesystem_write, sandbox_mode="sandboxed")

    results = await execute_tools(
        [ToolCall("c1", "filesystem_write", {"path": "blocked.txt", "content": "no"})],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="allow"),
    )

    assert results[0].status == "error"
    assert "Sandbox denied write access" in results[0].content
    assert not (workspace / "blocked.txt").exists()


@pytest.mark.asyncio
async def test_tool_failure_hook_fires(temp_workspace):
    registry = ToolRegistry()
    registry.register(failing_tool_tool, sandbox_mode="host")
    plugin_ctx = xcore.Context()
    calls = []

    async def failure(ctx):
        calls.append((ctx.tool_call.name, type(ctx.error).__name__, ctx.tool_result.status))

    plugin_ctx.on(Events.TOOL_CALL_FAILURE, failure)

    results = await execute_tools(
        [ToolCall("c1", "failing_tool", {})],
        registry,
        permission_system=PermissionSystem(default_decision="allow"),
        plugin_ctx=plugin_ctx,
        context_factory=_event_context,
    )

    assert results[0].status == "error"
    assert calls == [("failing_tool", "RuntimeError", "error")]


@pytest.mark.asyncio
async def test_before_tool_call_rewrite_updates_tool_id_and_resolves_paths(temp_workspace):
    registry = ToolRegistry()
    registry.register(filesystem_write, sandbox_mode="host")
    sandbox = SandboxPolicy(enabled=False, workspace_root=temp_workspace)
    plugin_ctx = xcore.Context()
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

    plugin_ctx.on(Events.BEFORE_TOOL_CALL, rewrite_tool_call)
    plugin_ctx.on(Events.AFTER_TOOL_CALL, after_tool_call)
    plugin_ctx.on(Events.POST_TOOL_BATCH, post_batch)

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
        plugin_ctx=plugin_ctx,
        context_factory=_event_context,
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
    plugin_ctx = xcore.Context()
    plugin_ctx.on(
        Events.AFTER_TOOLS,
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
        plugin_ctx=plugin_ctx,
        state_store=state_store,
        context_builder=ContextBuilder(),
        sandbox_policy=SandboxPolicy(enabled=False, workspace_root=str(temp_workspace)),
        permission_system=PermissionSystem(default_decision="allow"),
        config=RuntimeConfig(),
    )

    events = [e async for e in engine.run_turn("run large")]
    tool_event = next(e for e in events if e["type"] == "tool_result")
    tool_message = next(m for m in engine.messages if m.role == "tool")

    assert tool_event["data"]["content"].startswith("Tool result cached at session/")
    cached = ET.fromstring(tool_message.content)
    assert cached.tag == "cached_content"
    assert cached.attrib["kind"] == "tool_result"
    assert "x" * 100 not in tool_message.content
    assert cached.find("preview/ending") is not None
    assert cached.find("read_instruction") is not None
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
async def test_cache_hook_stores_original_text_instead_of_json_wrapper(state_store):
    hook = make_tool_result_cache_hook(
        state_store,
        max_inline_chars=100,
        preview_chars=20,
    )
    original = "line 1\n" + "source text\n" * 20
    data = {
        "ok": True,
        "path": "/workspace/source.py",
        "content": original,
    }
    message = Message(
        role="tool",
        content=original,
        tool_call_id="filesystem-read",
        data=data,
    )

    await hook(SimpleNamespace(tool_results=[message]))

    cache_dir = Path(state_store.artifacts_dir) / "tool_results"
    cache_files = list(cache_dir.glob("*.txt"))
    assert len(cache_files) == 1
    assert cache_files[0].read_text(encoding="utf-8") == original
    assert not list(cache_dir.glob("*.json"))
    cached = ET.fromstring(message.content)
    assert cached.tag == "cached_content"
    assert cached.attrib["original_chars"] == str(len(original))
    assert message.data == data
    assert message.artifact["kind"] == "cached_tool_result"
    assert message.artifact["cache_path"].endswith(cache_files[0].name)


@pytest.mark.asyncio
async def test_cache_hook_ignores_large_sidecar_data(state_store):
    hook = make_tool_result_cache_hook(
        state_store,
        max_inline_chars=100,
        preview_chars=20,
    )
    structured = {"items": [{"path": f"file-{index}.txt"} for index in range(30)]}
    message = Message(
        role="tool",
        content="30 files found.",
        tool_call_id="filesystem-list",
        data=structured,
    )

    await hook(SimpleNamespace(tool_results=[message]))

    assert message.content == "30 files found."
    assert message.data == structured
    assert not (Path(state_store.artifacts_dir) / "tool_results").exists()


@pytest.mark.asyncio
async def test_cache_hook_ignores_string_sidecar_data(state_store):
    hook = make_tool_result_cache_hook(
        state_store,
        max_inline_chars=20,
        preview_chars=10,
    )
    original = '{"already":"json text","lines":"a\\nb"}'
    message = Message(
        role="tool",
        content="Structured result.",
        tool_call_id="string-data",
        data=original,
    )

    await hook(SimpleNamespace(tool_results=[message]))

    assert message.content == "Structured result."
    assert message.data == original
    assert not (Path(state_store.artifacts_dir) / "tool_results").exists()


def _event_context(**kwargs):
    return EventContext(
        session=SessionInfo(session_id="s", thread_id="t", workspace_root="/workspace", provider="p"),
        **kwargs,
    )
