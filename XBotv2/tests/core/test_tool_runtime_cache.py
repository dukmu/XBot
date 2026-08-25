"""Tests for tool runtime path resolution and result caching."""

import json
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from XBotv2.coretools.filesystem import filesystem_tools
from XBotv2.coretools.filesystem import (
    edit,
    path,
    read,
    search,
)

filesystem_edit = next(t for t in filesystem_tools(None) if t.name == "edit")
filesystem_path = next(t for t in filesystem_tools(None) if t.name == "path")
filesystem_read = next(t for t in filesystem_tools(None) if t.name == "read")
filesystem_search = next(t for t in filesystem_tools(None) if t.name == "search")
from XBotv2.interactions.tools import build_ask_user_tool
from XBotv2.agentloop.engine import Engine
from XBotv2.context_builder.plugin import ContextBuilderComponent
import xcore
from XBotv2.agentloop import EventContext, Events
from XBotv2.session import SessionInfo
from XBotv2.agentloop import LoopSettings, LoopState
from XBotv2.core.messages import Message
from XBotv2.core.history import ConversationHistory
from XBotv2.core.artifacts import ArtifactKind
from XBotv2.permission_request import PermissionRequestData
from XBotv2.llm.mock import MockLLM
from XBotv2.permissions import PERMISSION_REQUESTED
from XBotv2.permissions.system import PermissionSystem
from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.coretools.result_cache import make_tool_result_cache_hook
from XBotv2.sandbox.policy import SandboxPolicy
from XBotv2.core.tools import ArtifactRef, Tool, ToolCall, ToolError, ToolResult
from XBotv2.permission_request.service import ApprovalService
from XBotv2.application.client_events import ClientEventRouter
from XBotv2.interactions.plugin import InteractionsService
from XBotv2.tests.helpers import make_tool_ctx


async def execute_tools(
    tool_calls,
    _registry,
    *,
    ctx,
    context_factory=None,
    **_obsolete,
):
    """Exercise the public agent-loop Tools service."""
    return await ctx.tools.execute_all(
        tool_calls,
        context_factory=context_factory,
    )


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
    registry.register(filesystem_edit)
    sandbox = SandboxPolicy(enabled=False, workspace_root=temp_workspace)

    ctx = make_tool_ctx(
        registry,
        sandbox=sandbox,
        permissions=PermissionSystem(default_decision="allow"),
    )
    results = await execute_tools(
        [
            ToolCall(
                "c1",
                "edit",
                {"path": str(temp_workspace / "out.txt"), "mode": "write", "content": "ok"},
            )
        ],
        registry,
        ctx=ctx,
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
    sandbox = SandboxPolicy(
        enabled=False,
        data_root=tmp_path / "data",
        workspace_root=workspace,
        session_root=session_root,
    )
    registry = ToolRegistry()
    registry.register(next(t for t in filesystem_tools(sandbox) if t.name == "read"))

    ctx = make_tool_ctx(
        registry,
        sandbox=sandbox,
        permissions=PermissionSystem(default_decision="allow"),
    )
    results = await execute_tools(
        [ToolCall("c1", "read", {
            "path": "session/artifacts/tool_results/cached.txt",
        })],
        registry,
        ctx=ctx,
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
    for tool in filesystem_tools(sandbox):
        if tool.name in {"read", "search"}:
            registry.register(tool)
    ctx = make_tool_ctx(
        registry,
        sandbox=sandbox,
        permissions=PermissionSystem(default_decision="allow"),
    )

    results = await execute_tools(
        [
            ToolCall("list", "read", {"path": "session/artifacts", "mode": "list"}),
            ToolCall("search", "search", {
                "path": "session/artifacts", "pattern": "cached",
            }),
            ToolCall("find", "search", {
                "path": "session/artifacts", "pattern": "*.txt", "mode": "name",
            }),
        ],
        registry,
        ctx=ctx,
    )

    assert all(result.status == "success" for result in results)
    assert "tool_results" in results[0].content
    assert "tool_results/cached.txt" in results[1].content
    assert "cached content" in results[1].content
    assert "tool_results/cached.txt" in results[2].content


@pytest.mark.asyncio
async def test_tool_without_sandbox_dependency_receives_none(temp_workspace):
    seen = []

    async def inspect_backend(*, sandbox=None):
        seen.append(sandbox)
        return "ok"

    sandbox = SandboxPolicy(enabled=True, workspace_root=temp_workspace)
    registry = ToolRegistry()
    registry.register(Tool.from_function(inspect_backend))

    ctx = make_tool_ctx(
        registry,
        sandbox=sandbox,
        permissions=PermissionSystem(default_decision="allow"),
    )
    results = await execute_tools(
        [ToolCall("c1", "inspect_backend", {})],
        registry,
        ctx=ctx,
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
    sandbox = SandboxPolicy(enabled=True, workspace_root=temp_workspace)
    async def bound_inspect_backend():
        return await inspect_backend(sandbox=sandbox)

    registry.register(Tool.from_function(bound_inspect_backend, name="inspect_backend"))

    ctx = make_tool_ctx(
        registry,
        sandbox=sandbox,
        permissions=PermissionSystem(default_decision="allow"),
    )
    results = await execute_tools(
        [ToolCall("c1", "inspect_backend", {})],
        registry,
        ctx=ctx,
    )

    assert results[0].status == "success"
    assert seen == [sandbox]


@pytest.mark.asyncio
async def test_permission_ask_without_approval_fails_closed(temp_workspace):
    registry = ToolRegistry()
    registry.register(filesystem_edit)
    sandbox = SandboxPolicy(enabled=False, workspace_root=temp_workspace)

    ctx = make_tool_ctx(
        registry,
        sandbox=sandbox,
        permissions=PermissionSystem(default_decision="ask"),
    )
    results = await execute_tools(
        [ToolCall("c1", "edit", {"path": "blocked.txt", "mode": "write", "content": "no"})],
        registry,
        ctx=ctx,
    )

    assert results[0].status == "error"
    assert results[0].content == "Error: Permission approval required for tool: edit."
    assert not (temp_workspace / "blocked.txt").exists()


@pytest.mark.asyncio
async def test_live_permission_allow_executes_current_tool_call(temp_workspace):
    registry = ToolRegistry()
    registry.register(filesystem_edit)
    sandbox = SandboxPolicy(enabled=False, workspace_root=temp_workspace)
    seen = []

    async def approve(event, **_kwargs):
        seen.append((event.type, event.data["request_id"]))
        return {"status": "answered", "decision": "allow", "scope": "once"}

    service_ctx = xcore.Context()
    client_events = ClientEventRouter()
    client_events.set_sink(approve)
    approval = ApprovalService(service_ctx, client_events)
    ctx = make_tool_ctx(
        registry,
        sandbox=sandbox,
        permissions=PermissionSystem(default_decision="ask"),
        approval=approval,
        base=service_ctx,
    )
    results = await execute_tools(
        [
            ToolCall(
                "c1",
                "edit",
                {"path": str(temp_workspace / "allowed.txt"), "mode": "write", "content": "ok"},
            )
        ],
        registry,
        ctx=ctx,
    )

    assert seen == [("permission_request", "permission:c1")]
    assert results[0].status == "success"
    assert (temp_workspace / "allowed.txt").read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
async def test_builtin_ask_user_rejects_empty_or_unstructured_options() -> None:
    called = False

    async def answer(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"status": "answered", "answer": "unused"}

    service_ctx = xcore.Context()
    client_events = ClientEventRouter()
    client_events.set_sink(answer)
    interactions = InteractionsService(service_ctx, client_events)
    registry = ToolRegistry()
    registry.register(build_ask_user_tool(interactions))
    ctx = make_tool_ctx(
        registry,
        permissions=PermissionSystem(default_decision="allow"),
        interactions=interactions,
        base=service_ctx,
    )
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
        ctx=ctx,
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
            error=ToolError("dict_error", "failed"),
            artifacts=(ArtifactRef("artifact-1", "text/plain", "result.txt"),),
        )

    registry = ToolRegistry()
    registry.register(
        Tool.from_function(structured_result),
    )

    ctx = make_tool_ctx(
        registry,
        permissions=PermissionSystem(default_decision="allow"),
    )
    results = await execute_tools(
        [ToolCall("c1", "structured_result", {})],
        registry,
        ctx=ctx,
    )

    assert results[0].status == "error"
    assert results[0].error["code"] == "dict_error"
    assert results[0].artifact[0].name == "result.txt"


@pytest.mark.asyncio
async def test_plain_dictionary_result_is_model_content() -> None:
    async def plain_result() -> dict:
        return {"data": {"value": 1}}

    registry = ToolRegistry()
    registry.register(Tool.from_function(plain_result))

    ctx = make_tool_ctx(
        registry,
        permissions=PermissionSystem(default_decision="allow"),
    )
    results = await execute_tools(
        [ToolCall("c1", "plain_result", {})],
        registry,
        ctx=ctx,
    )

    assert results[0].status == "success"
    assert json.loads(results[0].content) == {"data": {"value": 1}}


@pytest.mark.asyncio
async def test_permission_and_batch_hooks_fire(temp_workspace):
    registry = ToolRegistry()
    registry.register(filesystem_edit)
    sandbox = SandboxPolicy(enabled=False, workspace_root=temp_workspace)
    plugin_ctx = xcore.Context()
    calls = []

    async def permission_request(ctx):
        calls.append((
            "permission_request",
            ctx.tool_call.name,
            ctx.client_event.data["decision"],
        ))

    async def tool_denied(ctx):
        calls.append(("denied", ctx.tool_call.name, type(ctx.error).__name__))

    async def post_batch(ctx):
        calls.append(("batch", len(ctx.tool_calls), len(ctx.tool_results)))

    plugin_ctx.on(PERMISSION_REQUESTED, permission_request)
    plugin_ctx.on(Events.TOOL_DENIED, tool_denied)
    plugin_ctx.on(Events.POST_TOOL_BATCH, post_batch)

    ctx = make_tool_ctx(
        registry,
        sandbox=sandbox,
        permissions=PermissionSystem(default_decision="ask"),
        base=plugin_ctx,
    )
    results = await execute_tools(
        [ToolCall("c1", "edit", {"path": "blocked.txt", "mode": "write", "content": "no"})],
        registry,
        ctx=ctx,
        context_factory=_event_context,
    )

    assert results[0].status == "error"
    assert calls == [
        ("permission_request", "edit", "ask"),
        ("denied", "edit", "PermissionError"),
        ("batch", 1, 1),
    ]


@pytest.mark.asyncio
async def test_sandbox_external_read_readonly_allows_without_approval(tmp_path):
    """Sandbox is enforcement-only: readonly external reads need no approval."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("approved\n", encoding="utf-8")
    sandbox = SandboxPolicy(
        config={"external_read": "readonly", "external_write": "deny"},
        workspace_root=workspace,
    )
    if not sandbox.backend_available:
        pytest.skip("bubblewrap is not installed")
    registry = ToolRegistry()
    registry.register(filesystem_read)
    ctx = make_tool_ctx(
        registry,
        sandbox=sandbox,
        permissions=PermissionSystem(default_decision="allow"),
    )
    results = await execute_tools(
        [ToolCall("c1", "read", {"path": str(external)})],
        registry,
        ctx=ctx,
    )

    assert results[0].status == "success"
    assert results[0].content == "approved\n"


@pytest.mark.asyncio
async def test_sandbox_external_read_deny_fails_closed(tmp_path):
    """A denied sandbox policy hard-fails the call with no approval flow."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("blocked\n", encoding="utf-8")
    sandbox = SandboxPolicy(
        config={"external_read": "deny", "external_write": "deny"},
        workspace_root=workspace,
    )
    if not sandbox.backend_available:
        pytest.skip("bubblewrap is not installed")
    registry = ToolRegistry()
    registry.register(filesystem_read)
    ctx = make_tool_ctx(
        registry,
        sandbox=sandbox,
        permissions=PermissionSystem(default_decision="allow"),
    )
    results = await execute_tools(
        [ToolCall("c1", "read", {"path": str(external)})],
        registry,
        ctx=ctx,
    )

    assert results[0].status == "error"
    assert "Sandbox denied" in results[0].content
    assert results[0].client_events == []


@pytest.mark.asyncio
async def test_shell_can_request_sandbox_escalation_before_execution(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = SandboxPolicy(
        config={"external_write": "deny"},
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
    )
    events = []

    async def approve(event, **_kwargs):
        events.append(event.to_dict())
        return {"status": "answered", "decision": "allow", "scope": "once"}

    service_ctx = xcore.Context()
    client_events = ClientEventRouter()
    client_events.set_sink(approve)
    approval = ApprovalService(service_ctx, client_events)
    ctx = make_tool_ctx(
        registry,
        sandbox=sandbox,
        permissions=PermissionSystem(default_decision="allow"),
        approval=approval,
        base=service_ctx,
    )
    results = await execute_tools(
        [ToolCall("c1", "shell", {
            "sandbox_permissions": "require_escalated",
            "justification": "Install a required dependency.",
        })],
        registry,
        ctx=ctx,
    )

    assert results[0].status == "success"
    assert calls == [(
        "require_escalated",
        "Install a required dependency.",
    )]
    event = events[0]
    assert event["data"]["source"] == "permission_system"
    assert "Install a required dependency." in event["data"]["reason"]


@pytest.mark.asyncio
async def test_sandbox_copy_denied_when_external_destination_forbidden(tmp_path):
    """Sandbox enforcement checks both paths; a forbidden write fails closed."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("copy me\n", encoding="utf-8")
    sandbox = SandboxPolicy(
        config={"external_read": "readonly", "external_write": "deny"},
        workspace_root=workspace,
    )
    if not sandbox.backend_available:
        pytest.skip("bubblewrap is not installed")
    registry = ToolRegistry()
    registry.register(filesystem_path)
    events = []

    async def approve(event, **_kwargs):
        events.append(event)
        return {"status": "answered", "decision": "allow", "scope": "once"}

    ctx = make_tool_ctx(
        registry,
        sandbox=sandbox,
        permissions=PermissionSystem(default_decision="allow"),
    )
    results = await execute_tools(
        [ToolCall(
            "c1",
            "path",
            {
                "operation": "copy",
                "source": str(source),
                "destination": str(destination),
            },
        )],
        registry,
        ctx=ctx,
    )

    assert results[0].status == "error"
    assert "Sandbox denied" in results[0].content
    assert events == []
    assert not destination.exists()
    assert str(destination) in results[0].content


@pytest.mark.asyncio
async def test_sandbox_workspace_write_deny_fails_before_mutation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = SandboxPolicy(
        config={"workspace_read": "allow", "workspace_write": "deny"},
        workspace_root=workspace,
    )
    registry = ToolRegistry()
    registry.register(filesystem_edit)

    ctx = make_tool_ctx(
        registry,
        sandbox=sandbox,
        permissions=PermissionSystem(default_decision="allow"),
    )
    results = await execute_tools(
        [ToolCall("c1", "edit", {"path": "blocked.txt", "mode": "write", "content": "no"})],
        registry,
        ctx=ctx,
    )

    assert results[0].status == "error"
    assert "Sandbox denied write access" in results[0].content
    assert not (workspace / "blocked.txt").exists()


@pytest.mark.asyncio
async def test_tool_failure_hook_fires(temp_workspace):
    registry = ToolRegistry()
    registry.register(failing_tool_tool)
    plugin_ctx = xcore.Context()
    calls = []

    async def failure(ctx):
        calls.append((ctx.tool_call.name, type(ctx.error).__name__, ctx.tool_result.status))

    plugin_ctx.on(Events.TOOL_CALL_FAILURE, failure)

    ctx = make_tool_ctx(
        registry,
        permissions=PermissionSystem(default_decision="allow"),
        base=plugin_ctx,
    )
    results = await execute_tools(
        [ToolCall("c1", "failing_tool", {})],
        registry,
        ctx=ctx,
        context_factory=_event_context,
    )

    assert results[0].status == "error"
    assert calls == [("failing_tool", "RuntimeError", "error")]


@pytest.mark.asyncio
async def test_before_tool_call_rewrite_updates_tool_id_and_resolves_paths(temp_workspace):
    registry = ToolRegistry()
    registry.register(filesystem_edit)
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
                    "mode": "write",
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

    ctx = make_tool_ctx(
        registry,
        sandbox=sandbox,
        permissions=PermissionSystem(default_decision="allow"),
        base=plugin_ctx,
    )
    guard_calls = []

    def observe_guard(tool_call, _entry):
        guard_calls.append((tool_call.id, tool_call.name, dict(tool_call.args)))
        return None

    ctx.tools.guard(observe_guard)
    results = await execute_tools(
        [
            ToolCall(
                "old_id",
                "edit",
                {"path": "old.txt", "mode": "write", "content": "no"},
            )
        ],
        registry,
        ctx=ctx,
        context_factory=_event_context,
    )

    assert results[0].status == "success"
    assert results[0].tool_call_id == "rewritten_id"
    assert not (temp_workspace / "old.txt").exists()
    assert (temp_workspace / "rewritten.txt").read_text(encoding="utf-8") == "ok"
    assert guard_calls == [(
        "rewritten_id",
        "edit",
        {
            "path": str(temp_workspace / "rewritten.txt"),
            "mode": "write",
            "content": "ok",
        },
    )]
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
async def test_tool_receives_final_rewritten_tool_call() -> None:
    seen = []

    async def inspect(value: str, *, tool_call: ToolCall) -> str:
        seen.append(tool_call)
        return value

    plugin_ctx = xcore.Context()

    async def rewrite(ctx):
        return {
            "tool_call": ToolCall(
                "rewritten",
                ctx.tool_call.name,
                {"value": "updated"},
            )
        }

    plugin_ctx.on(Events.BEFORE_TOOL_CALL, rewrite)
    registry = ToolRegistry()
    registry.register(Tool.from_function(inspect))
    ctx = make_tool_ctx(
        registry,
        permissions=PermissionSystem(default_decision="allow"),
        base=plugin_ctx,
    )

    results = await execute_tools(
        [ToolCall("original", "inspect", {"value": "initial"})],
        registry,
        ctx=ctx,
        context_factory=_event_context,
    )

    assert results[0].content == "updated"
    assert seen == [ToolCall("rewritten", "inspect", {"value": "updated"})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shortcut",
    [
        {"deny_reason": "blocked"},
        {"tool_result": "cached"},
        "blocked",
    ],
)
async def test_before_tool_call_rejects_policy_shortcuts(
    shortcut,
    temp_workspace,
):
    registry = ToolRegistry()
    registry.register(large_output_tool)
    plugin_ctx = xcore.Context()

    async def short_circuit(_ctx):
        return shortcut

    plugin_ctx.on(Events.BEFORE_TOOL_CALL, short_circuit)
    ctx = make_tool_ctx(
        registry,
        sandbox=SandboxPolicy(
            enabled=False,
            workspace_root=temp_workspace,
        ),
        permissions=PermissionSystem(default_decision="allow"),
        base=plugin_ctx,
    )

    with pytest.raises(TypeError, match="BEFORE_TOOL_CALL"):
        await execute_tools(
            [ToolCall("c1", "large_output", {})],
            registry,
            ctx=ctx,
            context_factory=_event_context,
        )


@pytest.mark.asyncio
async def test_after_tools_cache_hook_truncates_before_history_and_events(
    state_store, artifact_store, temp_workspace
):
    registry = ToolRegistry()
    registry.register(large_output_tool)
    plugin_ctx = xcore.Context()
    plugin_ctx.on(
        Events.AFTER_TOOLS,
        make_tool_result_cache_hook(
            artifact_store,
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
    make_tool_ctx(
        registry,
        sandbox=SandboxPolicy(
            enabled=False,
            workspace_root=str(temp_workspace),
        ),
        permissions=PermissionSystem(default_decision="allow"),
        base=plugin_ctx,
    )
    ContextBuilderComponent().apply(plugin_ctx)
    state = LoopState(
        session=SessionInfo(
            session_id=state_store.session_id,
            thread_id=state_store.thread_id,
            workspace_root=str(temp_workspace),
            provider="default",
        ),
        messages=ConversationHistory(
            state_store.history.load(),
            sink=state_store.history,
        ),
    )
    engine = Engine(
        model_client=llm,
        tools=plugin_ctx.tools,
        events=plugin_ctx,
        state=state,
        settings=LoopSettings(provider="default", workspace=str(temp_workspace)),
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
    assert len(tool_message.artifact) == 1
    assert tool_message.artifact[0].kind is ArtifactKind.TOOL_RESULT
    assert not Path(artifact_store.model_path(tool_message.artifact[0])).is_absolute()
    assert str(state_store.paths.state_dir) not in tool_message.content

    cache_files = list(
        state_store.paths.artifact_dir(ArtifactKind.TOOL_RESULT).glob("*.txt")
    )
    assert len(cache_files) == 1
    assert cache_files[0].read_text(encoding="utf-8") == "x" * 200

    restored_tool_message = next(
        m for m in state_store.history.load() if m.role == "tool"
    )
    assert restored_tool_message.artifact == tool_message.artifact


@pytest.mark.asyncio
async def test_cache_hook_stores_original_text_instead_of_json_wrapper(
    state_store, artifact_store
):
    hook = make_tool_result_cache_hook(
        artifact_store,
        max_inline_chars=100,
        preview_chars=20,
    )
    original = "line 1\n" + "source text\n" * 20
    message = Message(
        role="tool",
        content=original,
        tool_call_id="filesystem-read",
    )

    await hook(SimpleNamespace(tool_results=[message]))

    cache_dir = state_store.paths.artifact_dir(ArtifactKind.TOOL_RESULT)
    cache_files = list(cache_dir.glob("*.txt"))
    assert len(cache_files) == 1
    assert cache_files[0].read_text(encoding="utf-8") == original
    assert not list(cache_dir.glob("*.json"))
    cached = ET.fromstring(message.content)
    assert cached.tag == "cached_content"
    assert cached.attrib["original_chars"] == str(len(original))
    assert len(message.artifact) == 1
    assert message.artifact[0].kind is ArtifactKind.TOOL_RESULT
    assert message.artifact[0].id.endswith(cache_files[0].name)


@pytest.mark.asyncio
async def test_cache_hook_ignores_large_sidecar_data(
    state_store, artifact_store
):
    hook = make_tool_result_cache_hook(
        artifact_store,
        max_inline_chars=100,
        preview_chars=20,
    )
    message = Message(
        role="tool",
        content="30 files found.",
        tool_call_id="filesystem-list",
    )

    await hook(SimpleNamespace(tool_results=[message]))

    assert message.content == "30 files found."
    assert not state_store.paths.artifact_dir(ArtifactKind.TOOL_RESULT).exists()


@pytest.mark.asyncio
async def test_cache_hook_ignores_string_sidecar_data(
    state_store, artifact_store
):
    hook = make_tool_result_cache_hook(
        artifact_store,
        max_inline_chars=20,
        preview_chars=10,
    )
    message = Message(
        role="tool",
        content="Structured result.",
        tool_call_id="string-data",
    )

    await hook(SimpleNamespace(tool_results=[message]))

    assert message.content == "Structured result."
    assert not state_store.paths.artifact_dir(ArtifactKind.TOOL_RESULT).exists()


def _event_context(**kwargs):
    return EventContext(
        session=SessionInfo(session_id="s", thread_id="t", workspace_root="/workspace", provider="p"),
        **kwargs,
    )
