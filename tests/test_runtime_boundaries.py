"""Focused runtime boundary tests for Hermes."""

import shutil
import json
import yaml

import pytest

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import tool as lc_tool
from langgraph.checkpoint.memory import MemorySaver

from tests.test_personality_runtime import write_local_runtime
from xbot.config import configure_runtime_paths
from xbot.context import get_system_prompt
from xbot.models import PermissionConfig, SandboxConfig, UserContext
from xbot.permissions import PermissionSystem
from tests.conftest import make_default_hooks, make_default_registry
from xbot.sandbox import SandboxPolicy, reset_runtime_sandbox, set_runtime_sandbox
from xbot.runtime import RuntimeContext
from xbot.state import (
    TaskStateStore,
    configure_runtime_task_state,
    reset_runtime_task_state,
)
from xbot.state_projection import materialize_context_tree_state, materialize_mailbox_state, read_jsonl
from xbot.builtin_tools import (
    compact,
    context_head,
    context_rewind,
    debug_analyze,
    filesystem_read,
    filesystem_write,
    mailbox_read,
    mailbox_send,
    memory_list,
    memory_search,
    plan_add_nodes,
    plan_autofill,
    plan_node_history,
    plan_next,
    plan_update,
    shell,
    summary_add,
    summary_list,
    summary_read,
    subagent_create,
    subagent_wait,
    task_begin,
    task_exit,
    task_status,
)
from xbot.cache import ToolResultCache
from xbot.planning import materialize_plan_state, select_ready_node, validate_plan
from xbot.verification import verification_passed, verify_task_state

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
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
        )


def test_system_prompt_contains_task_mode_operating_rules(temp_data_dir):
    """The model should receive task-mode operating rules, not only task tools."""
    write_local_runtime(temp_data_dir)
    configure_runtime_paths(data_dir=temp_data_dir, session_id="default", personality_id="default")

    user_ctx = UserContext(user_id="u", user_name="User", platform="test", session_type="private")
    prompt = get_system_prompt(
        user_context=user_ctx,
        agent_role="A helpful assistant",
        sandbox_summary="sandbox disabled",
    )

    assert "enter task mode with task_begin" in prompt
    assert "plan_autofill or plan_add_nodes" in prompt
    assert "drive the DAG through plan_next and plan_update" in prompt
    assert "task_status reports completion_errors" in prompt
    assert "plan_update summary/result/evidence_refs" in prompt
    assert "memory_update only for durable user/project facts" in prompt


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
        assert paths.state_dir == temp_data_dir / "sessions" / "analysis" / "state"
        assert paths.saver_dir == temp_data_dir / "sessions" / "analysis" / "saver"
        assert paths.langgraph_checkpoint_path == temp_data_dir / "sessions" / "analysis" / "saver" / "langgraph.pkl"
        assert paths.personality_dir == temp_data_dir / "personalities" / "hermes"
        assert "sessions/analysis/workspace" in resource_paths
        assert "sessions/analysis/state" in resource_paths
        assert "sessions/analysis/tasks" not in resource_paths
        assert "personalities/hermes/memory.md" in resource_paths
    finally:
        configure_runtime_paths(
            data_dir=original.data_dir,
            session_id=original.session_id,
            personality_id=original.personality_id,
        )


def test_runtime_paths_are_context_local(temp_data_dir):
    """Runtime paths should not be a single process-global mutable value."""
    from contextvars import copy_context
    from xbot.config import configure_runtime_paths, get_runtime_paths

    def configure_and_read(session_id: str) -> str:
        configure_runtime_paths(data_dir=temp_data_dir, session_id=session_id, personality_id="default")
        return get_runtime_paths().session_id

    ctx_a = copy_context()
    ctx_b = copy_context()

    assert ctx_a.run(configure_and_read, "session-a") == "session-a"
    assert ctx_b.run(configure_and_read, "session-b") == "session-b"
    assert ctx_a.run(lambda: get_runtime_paths().session_id) == "session-a"
    assert ctx_b.run(lambda: get_runtime_paths().session_id) == "session-b"


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
                    {"path": "personalities/{{personality_id}}/memory.md"},
                ]
            }
        )

        assert expanded == {
            "resources": [
                {"path": "sessions/analysis/workspace"},
                {"path": "personalities/hermes/memory.md"},
            ]
        }
    finally:
        configure_runtime_paths(
            data_dir=original.data_dir,
            session_id=original.session_id,
            personality_id=original.personality_id,
        )


def test_interaction_startup_uses_explicit_runtime_context(user_context, temp_data_dir):
    """Interaction status should derive session/personality from RuntimeContext when provided."""
    from xbot.config import RuntimePaths
    from xbot.interaction import HermesInteraction

    runtime_context = RuntimeContext(
        paths=RuntimePaths(data_dir=temp_data_dir, session_id="ctx-session", personality_id="ctx-personality"),
        thread_id="ctx-thread",
        task_id="ctx-thread",
        run_id="run_ctx",
        trace_id="trace_ctx",
    )
    runtime = HermesInteraction(
        user_context=user_context,
        agent_config=type("AgentCfg", (), {"name": "test", "max_context_tokens": 8000})(),
        provider_config=type("ProviderCfg", (), {"name": "mock", "model": "mock"})(),
        graph=None,
        graph_config={"configurable": {"thread_id": "ctx-thread"}},
        sandbox=SandboxPolicy(SandboxConfig(enabled=False)),
        tools=[],
        database_path=":memory:",
        runtime_context=runtime_context,
    )

    payloads = [str(event.payload) for event in runtime.startup_events()]

    assert "Session: ctx-session" in payloads
    assert "Personality: ctx-personality" in payloads


def test_task_state_store_initializes_file_backed_state(temp_data_dir):
    """A state directory should expose the file-as-state contract."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="analysis/thread",
        session_id="default",
        personality_id="default",
        goal="# Goal\n\nAnalyze runtime state.\n",
    )

    assert store.paths.task_yaml.exists()
    assert store.paths.goal_md.exists()
    assert store.paths.plan_yaml.exists()
    assert store.paths.graph_jsonl.exists()
    assert store.paths.state_yaml.exists()
    assert store.paths.context_md.exists()
    assert store.paths.context_tree_jsonl.exists()
    assert store.paths.mailbox_jsonl.exists()
    assert store.paths.artifacts_dir.exists()
    assert yaml.safe_load(store.paths.task_yaml.read_text(encoding="utf-8"))["mode"] == "chat"


@pytest.mark.asyncio
async def test_filesystem_read_locates_pattern_with_line_context(temp_data_dir):
    """Read tool should support targeted line/pattern inspection."""
    workspace = temp_data_dir / "sessions" / "default" / "workspace"
    target = workspace / "module.py"
    target.write_text("def a():\n    pass\n\ndef target():\n    return 42\n", encoding="utf-8")
    sandbox = SandboxPolicy(SandboxConfig(enabled=False), data_root=temp_data_dir, workspace_root=workspace)
    token = set_runtime_sandbox(sandbox)
    try:
        located = await filesystem_read.ainvoke({"path": "module.py", "pattern": "target", "context_lines": 1})
        ranged = await filesystem_read.ainvoke({"path": "module.py", "line_start": 4, "line_end": 5})
    finally:
        reset_runtime_sandbox(token)

    assert "@@ match:4 lines 3-5 @@" in located
    assert "4: def target():" in located
    assert "@@ range lines 4-5 @@" in ranged
    assert "5:     return 42" in ranged


@pytest.mark.asyncio
async def test_task_mode_tools_drive_goal_plan_and_context(temp_data_dir):
    """Task mode should make goal, DAG, status, and context.md actionable."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="task-mode",
        session_id="default",
        personality_id="default",
    )
    token = configure_runtime_task_state(store)
    try:
        started = await task_begin.ainvoke(
            {
                "goal": "Refactor a small project",
                "steps_json": '["Inspect files", "Edit implementation"]',
            }
        )
        first = await plan_next.ainvoke({})
        updated = await plan_update.ainvoke({"node_id": "n001", "status": "verified", "reason": "inspection done"})
        added = await plan_add_nodes.ainvoke(
            {
                "nodes_json": '[{"id":"n_verify","type":"verification","title":"Run tests","depends_on":["n002"],"status":"pending"}]',
                "reason": "need verification",
            }
        )
        second = await plan_next.ainvoke({})
        await plan_update.ainvoke({"node_id": "n002", "status": "verified", "reason": "implementation done"})
        verify = await plan_next.ainvoke({})
        await plan_update.ainvoke({"node_id": "n_verify", "status": "verified", "reason": "tests passed"})
        status = json.loads(await task_status.ainvoke({}))
        exited = json.loads(await task_exit.ainvoke({"status": "completed", "reason": "done"}))
    finally:
        reset_runtime_task_state(token)

    assert json.loads(started)["mode"] == "task"
    assert json.loads(first)["node"]["id"] == "n001"
    assert json.loads(second)["node"]["id"] == "n002"
    assert json.loads(verify)["node"]["id"] == "n_verify"
    assert json.loads(updated)["result"] == "n001 marked verified"
    assert "n_verify" in json.loads(added)["added"]
    assert "Refactor a small project" in store.paths.goal_md.read_text(encoding="utf-8")
    context = store.paths.context_md.read_text(encoding="utf-8")
    assert "## Active DAG Node" in context
    assert "n_verify" in context
    assert status["mode"] == "task"
    assert status["completion_errors"] == []
    assert exited["mode"] == "chat"


@pytest.mark.asyncio
async def test_task_begin_rejects_mixed_steps_json_without_silent_filtering(temp_data_dir):
    """steps_json parsing should validate shape only and never silently drop items."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="mixed-steps-json",
        session_id="default",
        personality_id="default",
    )
    token = configure_runtime_task_state(store)
    try:
        with pytest.raises(ValueError, match="only strings or only node objects"):
            await task_begin.ainvoke(
                {
                    "goal": "Reject malformed seed data",
                    "steps_json": '["Inspect", {"id":"n002","title":"Implement"}]',
                }
            )
    finally:
        reset_runtime_task_state(token)


@pytest.mark.asyncio
async def test_task_mode_enforces_plan_scope_and_completion(temp_data_dir):
    """Plan tools should be task-scoped, and completed exit should require a finished DAG."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="task-mode-guards",
        session_id="default",
        personality_id="default",
    )
    token = configure_runtime_task_state(store)
    try:
        with pytest.raises(ValueError, match="outside task mode"):
            await plan_add_nodes.ainvoke({"nodes_json": '[{"id":"n001","title":"Orphan"}]'})

        await task_begin.ainvoke({"goal": "Guarded task", "steps_json": '["Do work"]'})
        status = json.loads(await task_status.ainvoke({}))
        assert status["completion_errors"]
        with pytest.raises(ValueError, match="unfinished plan nodes"):
            await task_exit.ainvoke({"status": "completed"})

        await plan_next.ainvoke({})
        await plan_update.ainvoke({"node_id": "n001", "status": "verified"})
        exited = json.loads(await task_exit.ainvoke({"status": "completed"}))
    finally:
        reset_runtime_task_state(token)

    assert exited["mode"] == "chat"
    assert exited["status"] == "completed"


@pytest.mark.asyncio
async def test_plan_next_keeps_single_running_node(temp_data_dir):
    """The DAG scheduler should not start a second node while one is running."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="single-running-node",
        session_id="default",
        personality_id="default",
    )
    token = configure_runtime_task_state(store)
    try:
        await task_begin.ainvoke({"goal": "Single active node", "steps_json": '["First", "Second"]'})
        first = json.loads(await plan_next.ainvoke({}))
        second = json.loads(await plan_next.ainvoke({}))
        state = store.materialize_state()["plan"]
    finally:
        reset_runtime_task_state(token)

    assert first["node"]["id"] == "n001"
    assert second["node"]["id"] == "n001"
    assert second["node"]["already_running"] is True
    assert state["running_nodes"] == ["n001"]
    assert "n002" in state["ready_nodes"]


@pytest.mark.asyncio
async def test_plan_autofill_adds_standard_dag_skeleton(temp_data_dir):
    """Autofill should give a task a usable inspect/implement/verify/report DAG."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="autofill-plan",
        session_id="default",
        personality_id="default",
    )
    token = configure_runtime_task_state(store)
    try:
        await task_begin.ainvoke({"goal": "Refactor payment module"})
        filled = json.loads(
            await plan_autofill.ainvoke(
                {
                    "scope": "refactor",
                    "constraints_json": '{"checks":["unit tests pass"],"artifacts":["patch is scoped"]}',
                }
            )
        )
        duplicate = json.loads(await plan_autofill.ainvoke({"scope": "refactor"}))
        state = store.materialize_state()["plan"]
    finally:
        reset_runtime_task_state(token)

    assert filled["added"] == ["n_inspect", "n_implement", "n_verify", "n_report"]
    assert "added" not in duplicate
    assert duplicate["result"] == "standard DAG already present"
    assert state["active_node"] == "n_inspect"
    assert state["ready_nodes"] == ["n_inspect"]
    plan = yaml.safe_load(store.paths.plan_yaml.read_text(encoding="utf-8"))
    by_id = {node["id"]: node for node in plan["nodes"]}
    assert by_id["n_implement"]["depends_on"] == ["n_inspect"]
    assert by_id["n_verify"]["depends_on"] == ["n_implement"]
    assert "unit tests pass" in by_id["n_verify"]["success_criteria"]
    assert "patch is scoped" in by_id["n_implement"]["success_criteria"]

@pytest.mark.asyncio
async def test_plan_update_records_node_result_summary_and_evidence(temp_data_dir):
    """DAG nodes should carry their own execution facts."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="node-facts",
        session_id="default",
        personality_id="default",
    )
    token = configure_runtime_task_state(store)
    try:
        await task_begin.ainvoke({"goal": "Complete with node evidence"})
        await plan_autofill.ainvoke({"scope": "refactor"})
        await plan_next.ainvoke({})
        updated = json.loads(
            await plan_update.ainvoke(
                {
                    "node_id": "n_inspect",
                    "status": "verified",
                    "reason": "inspection done",
                    "summary": "Inspected target files.",
                    "result": "calculator.py is the only target.",
                    "evidence_refs_json": '["filesystem_read:calculator.py"]',
                    "changed_files_json": '["calculator.py"]',
                }
            )
        )
        context = store.paths.context_md.read_text(encoding="utf-8")
        node = next(node for node in store.plan_store.load_plan()["nodes"] if node["id"] == "n_inspect")
    finally:
        reset_runtime_task_state(token)

    assert updated["plan_version"] >= 3
    assert updated["next_action"]["action"] == "plan_next"
    assert node["summary"] == "Inspected target files."
    assert node["result"] == "calculator.py is the only target."
    assert node["evidence_refs"] == ["filesystem_read:calculator.py"]
    assert node["changed_files"] == ["calculator.py"]
    assert "summary: Inspected target files." in context
    assert "evidence: filesystem_read:calculator.py" in context


@pytest.mark.asyncio
async def test_plan_update_accepts_object_form_evidence_args(temp_data_dir):
    """OpenAI-compatible local models sometimes send object-shaped evidence args."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="node-evidence-object-args",
        session_id="default",
        personality_id="default",
    )
    token = configure_runtime_task_state(store)
    try:
        await task_begin.ainvoke({"goal": "Record object-shaped evidence"})
        await plan_add_nodes.ainvoke(
            {
                "nodes_json": '[{"id":"n001","title":"Do work","depends_on":[],"status":"ready"}]',
            }
        )
        await plan_update.ainvoke(
            {
                "node_id": "n001",
                "status": "verified",
                "evidence_refs_json": '{"file":"calculator.py"}',
                "changed_files_json": '{"calculator.py":"modified"}',
            }
        )
        node = next(node for node in store.plan_store.load_plan()["nodes"] if node["id"] == "n001")
    finally:
        reset_runtime_task_state(token)

    assert node["evidence_refs"] == ["file:calculator.py"]
    assert node["changed_files"] == ["calculator.py:modified"]


@pytest.mark.asyncio
async def test_task_completion_uses_dag_state(temp_data_dir):
    """Completed exit should be controlled by DAG node status."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="completion-with-node-evidence",
        session_id="default",
        personality_id="default",
    )
    token = configure_runtime_task_state(store)
    try:
        await task_begin.ainvoke({"goal": "Complete with node evidence", "steps_json": '["Do work"]'})
        await plan_next.ainvoke({})
        await plan_update.ainvoke(
            {
                "node_id": "n001",
                "status": "verified",
                "summary": "Work completed.",
                "result": "DAG node carries completion evidence.",
                "evidence_refs_json": '["node:n001"]',
            }
        )
        exited = json.loads(await task_exit.ainvoke({"status": "completed"}))
    finally:
        reset_runtime_task_state(token)

    assert exited["status"] == "completed"


@pytest.mark.asyncio
async def test_task_status_reports_next_action(temp_data_dir):
    """Task status should guide the agent toward the next DAG operation."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="task-next-action",
        session_id="default",
        personality_id="default",
    )
    token = configure_runtime_task_state(store)
    try:
        chat_status = json.loads(await task_status.ainvoke({}))
        await task_begin.ainvoke({"goal": "Guide next action", "steps_json": '["Inspect"]'})
        ready_status = json.loads(await task_status.ainvoke({}))
        await plan_next.ainvoke({})
        running_status = json.loads(await task_status.ainvoke({}))
        await plan_update.ainvoke({"node_id": "n001", "status": "verified"})
        complete_status = json.loads(await task_status.ainvoke({}))
        debug = json.loads(await debug_analyze.ainvoke({}))
    finally:
        reset_runtime_task_state(token)

    assert chat_status["next_action"]["action"] == "task_begin"
    assert ready_status["next_action"]["action"] == "plan_next"
    assert ready_status["next_action"]["node_id"] == "n001"
    assert running_status["next_action"]["action"] == "plan_update"
    assert running_status["next_action"]["node_id"] == "n001"
    assert complete_status["next_action"]["action"] == "task_exit"
    assert debug["task"]["next_action"]["action"] == "task_exit"


@pytest.mark.asyncio
async def test_dag_events_are_attributed_to_active_plan_node(temp_data_dir):
    """Turn, tool, artifact, and summary events should point at the active DAG node."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="dag-attribution",
        session_id="default",
        personality_id="default",
    )
    token = configure_runtime_task_state(store)
    try:
        await task_begin.ainvoke({"goal": "Ship feature", "steps_json": '["Inspect"]'})
        await plan_next.ainvoke({})
        store.record_turn_started(turn_id="turn_000001", input_kind="user_message", content="work")
        store.record_turn_events(
            turn_id="turn_000001",
            events=[
                type("Evt", (), {"kind": "tool_call", "source": "agent", "payload": {"name": "filesystem_read"}})(),
                type("Evt", (), {"kind": "message", "source": "agent", "payload": "done"})(),
            ],
        )
        store.record_summary(content="Inspection finished.", reason="node progress", source="test")
        history = json.loads(await plan_node_history.ainvoke({"node_id": "n001"}))
        debug = json.loads(await debug_analyze.ainvoke({"scope": "dag"}))
    finally:
        reset_runtime_task_state(token)

    graph_events = list(read_jsonl(store.paths.graph_jsonl))
    assert any(event.get("plan_node_id") == "n001" and event.get("event") == "tool_call_observed" for event in graph_events)
    assert any(event.get("plan_node_id") == "n001" and event.get("type") == "summary" for event in graph_events)
    assert store.materialize_state()["dag"]["node_event_counts"]["n001"] >= 3
    assert any(event.get("plan_node_id") == "n001" for event in history)
    assert any(node["id"] == "n001" for node in debug["plan"]["nodes"])
    assert debug["dag"]["activity"]["node_event_counts"]["n001"] >= 3
    assert "tool_call_observed" in debug["dag"]["event_counts_by_node_and_type"]["n001"]


@pytest.mark.asyncio
async def test_summary_tools_project_into_context(temp_data_dir):
    """Summaries should be durable artifacts and visible in context.md."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="summary-tools",
        session_id="default",
        personality_id="default",
    )
    token = configure_runtime_task_state(store)
    try:
        added = json.loads(await summary_add.ainvoke({"content": "User prefers DAG-first execution.", "reason": "preference"}))
        listed = json.loads(await summary_list.ainvoke({"limit": 2}))
        content = await summary_read.ainvoke({"summary_id": added["summary_id"]})
    finally:
        reset_runtime_task_state(token)

    context = store.paths.context_md.read_text(encoding="utf-8")
    state = store.materialize_state()
    assert added["summary_id"] == "summary_000001"
    assert added["reason"] == "preference"
    assert listed[0]["summary_id"] == "summary_000001"
    assert listed[0]["reason"] == "preference"
    assert content.startswith("---\n")
    assert "summary_id: summary_000001" in content
    assert "DAG-first" in content
    assert "## Recent Summaries" in context
    assert "DAG-first" in context
    assert state["summaries"]["count"] == 1
    assert state["summaries"]["latest"]["reason"] == "preference"


def test_project_context_includes_pending_mailbox(temp_data_dir):
    """Pending mailbox items should influence task context projection."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="mailbox-context",
        session_id="default",
        personality_id="default",
    )
    store.send_mailbox_message(sender="runtime", recipient="agent", subject="review", content="Check active node")
    context = store.paths.context_md.read_text(encoding="utf-8")

    assert "## Pending Mailbox" in context
    assert "Check active node" in context


async def test_memory_tools_list_and_search_structured_entries(temp_data_dir):
    """Memory should be searchable instead of append-only opaque text."""
    configure_runtime_paths(data_dir=temp_data_dir, session_id="default", personality_id="default")
    memory_path = temp_data_dir / "personalities" / "default" / "memory.md"
    memory_path.write_text(
        "---\nts: 2026-06-02T00:00:00+00:00\ncontent: |\n  User prefers DAG-first execution.\n"
        "---\nts: 2026-06-02T00:01:00+00:00\ncontent: |\n  Keep ROS teaching mode separate from coding mode.\n",
        encoding="utf-8",
    )

    listed = json.loads(await memory_list.ainvoke({"limit": 2}))
    found = json.loads(await memory_search.ainvoke({"query": "ROS teaching"}))

    assert len(listed) == 2
    assert listed[0]["id"] == "mem_000001"
    assert found[0]["content"] == "Keep ROS teaching mode separate from coding mode."


def test_task_state_store_materializes_events(temp_data_dir):
    """state.yaml should be a materialized view of append-only runtime events."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="materialize",
        session_id="default",
        personality_id="default",
    )

    store.record_turn_started(turn_id="turn_000001", input_kind="user_message", content="hello")
    store.record_turn_events(
        turn_id="turn_000001",
        events=[
            type("Evt", (), {"kind": "message", "source": "agent", "payload": "ok"})(),
            type("Evt", (), {"kind": "tool_call", "source": "agent", "payload": {"name": "shell", "args": {"command": "pwd"}}})(),
        ],
    )
    store.record_turn_finished(turn_id="turn_000001", status="completed")

    state = yaml.safe_load(store.paths.state_yaml.read_text(encoding="utf-8"))
    events = list(read_jsonl(store.paths.events_jsonl))
    graph_events = list(read_jsonl(store.paths.graph_jsonl))

    assert state["turn_count"] == 1
    assert state["event_count"] == len(events)
    assert state["graph_event_count"] == len(graph_events)
    assert state["context_tree_event_count"] == 3
    assert state["context_tree"]["node_count"] == 3
    assert state["context_tree"]["head"] == "ctx_000003"
    assert state["interaction_event_counts"]["by_kind"]["message"] == 1
    assert state["interaction_event_counts"]["by_kind"]["tool_call"] == 1
    assert events[-1]["type"] == "turn_finished"


def test_task_state_store_batches_materialized_state_writes(temp_data_dir):
    """Batching should avoid rewriting state.yaml for every event projection."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="materialize-batch",
        session_id="default",
        personality_id="default",
    )
    original = store.write_materialized_state
    calls = 0

    def counted_write():
        nonlocal calls
        calls += 1
        return original()

    store.write_materialized_state = counted_write
    store.record_turn_events(
        turn_id="turn_000001",
        events=[
            type("Evt", (), {"kind": "message", "source": "agent", "payload": "one"})(),
            type("Evt", (), {"kind": "tool_call", "source": "agent", "payload": {"name": "shell"}})(),
            type("Evt", (), {"kind": "message", "source": "agent", "payload": "two"})(),
        ],
    )

    assert calls == 1


def test_context_tree_rewind_moves_head_without_deleting_history(temp_data_dir):
    """Rewind should move the context head while preserving append-only history."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="context-rewind",
        session_id="default",
        personality_id="default",
    )

    first = store.record_context_node(turn_id="turn_1", kind="user_message", source="user", payload="first")
    second = store.record_context_node(turn_id="turn_1", kind="message", source="agent", payload="second")
    store.record_context_node(turn_id="turn_2", kind="message", source="agent", payload="branch")
    store.rewind_context(first["node_id"], reason="try alternate branch")

    tree_events = list(read_jsonl(store.paths.context_tree_jsonl))
    state = yaml.safe_load(store.paths.state_yaml.read_text(encoding="utf-8"))
    projected = materialize_context_tree_state(tree_events)

    assert first["node_id"] == "ctx_000001"
    assert second["parent_id"] == first["node_id"]
    assert state["context_tree"]["head"] == first["node_id"]
    assert state["context_tree"]["node_count"] == 3
    assert state["context_tree"]["rewind_count"] == 1
    assert projected["errors"] == []
    assert any(event.get("type") == "context_rewind" for event in read_jsonl(store.paths.events_jsonl))


def test_mailbox_state_tracks_pending_and_acknowledged_messages(temp_data_dir):
    """Mailbox state should be append-only and materialized by recipient."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="mailbox-state",
        session_id="default",
        personality_id="default",
    )

    first = store.send_mailbox_message(sender="parent", recipient="subagent:a", subject="work", content="Refactor file")
    store.send_mailbox_message(sender="subagent:a", recipient="parent", subject="done", content="Patch ready")
    store.acknowledge_mailbox_message(first["message_id"], actor="subagent:a")

    mailbox_events = list(read_jsonl(store.paths.mailbox_jsonl))
    mailbox = materialize_mailbox_state(mailbox_events)
    state = yaml.safe_load(store.paths.state_yaml.read_text(encoding="utf-8"))

    assert mailbox["message_count"] == 2
    assert mailbox["acknowledged_count"] == 1
    assert mailbox["pending_by_recipient"] == {"parent": 1}
    assert state["mailbox"]["pending_count"] == 1
    assert state["mailbox_event_count"] == 3
    assert any(event.get("type") == "mailbox_message_sent" for event in read_jsonl(store.paths.events_jsonl))


@pytest.mark.asyncio
async def test_mailbox_tools_use_bound_task_state(temp_data_dir):
    """Agent-facing mailbox tools should read and acknowledge pending messages."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="mailbox-tools",
        session_id="default",
        personality_id="default",
    )
    token = configure_runtime_task_state(store)
    try:
        sent = await mailbox_send.ainvoke(
            {"recipient": "agent", "subject": "notice", "content": "hello", "sender": "runtime"}
        )
        pending = await mailbox_read.ainvoke({"recipient": "agent"})
        acknowledged = await mailbox_read.ainvoke({"recipient": "agent", "acknowledge": True})
        empty = await mailbox_read.ainvoke({"recipient": "agent"})
    finally:
        reset_runtime_task_state(token)

    assert "Mailbox message sent: msg_000001" in sent
    assert '"subject": "notice"' in pending
    assert '"acknowledged": true' in acknowledged
    assert empty == "[]"


@pytest.mark.asyncio
async def test_attach_subagent_runs_child_runtime_and_reports_via_mailbox(temp_data_dir):
    """Attach-mode subagent should execute in a child runtime when agent state is bound."""
    write_local_runtime(temp_data_dir)
    configure_runtime_paths(data_dir=temp_data_dir, session_id="parent", personality_id="default")
    parent_workspace = temp_data_dir / "sessions" / "parent" / "workspace"
    parent_workspace.mkdir(parents=True)
    (parent_workspace / "calculator.py").write_text("def add(a, b):\n    return a+b\n", encoding="utf-8")
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "parent" / "tasks",
        thread_id="parent-task",
        session_id="parent",
        personality_id="default",
    )
    sandbox = SandboxPolicy(SandboxConfig(enabled=False), data_root=temp_data_dir, workspace_root=parent_workspace)
    state_token = configure_runtime_task_state(store)
    sandbox_token = set_runtime_sandbox(sandbox)
    try:
        result = await subagent_create.ainvoke(
            {"task": "Refactor calculator.py to improve readability.", "name": "worker", "mode": "attach"}
        )
        wait_result = None
    finally:
        reset_runtime_sandbox(sandbox_token)
        reset_runtime_task_state(state_token)

    assert "Subagent worker_" in result
    assert "completed" in result
    assert not list((temp_data_dir / "sessions").glob("parent__subagent__worker_*"))
    assert "return a + b" in (parent_workspace / "calculator.py").read_text(encoding="utf-8")
    mailbox = store.materialize_state()["mailbox"]
    assert mailbox["pending_by_recipient"]["parent"] == 1
    manifests = sorted((temp_data_dir / "sessions" / "parent" / "subagents").glob("worker_*/manifest.json"))
    assert manifests
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    subagent_id = manifest["subagent_id"]
    assert manifest["child_session_id"] == "parent"
    assert manifest["child_thread_id"] == f"subagent-{subagent_id}"
    assert manifest["workspace"] == str(parent_workspace)
    assert manifest["child_task_state"] == str(temp_data_dir / "sessions" / "parent" / "subagents" / subagent_id / "state")
    assert (temp_data_dir / "sessions" / "parent" / "subagents" / subagent_id / "state" / "plan.yaml").exists()
    assert (temp_data_dir / "sessions" / "parent" / "subagents" / subagent_id / "saver" / "langgraph.pkl").exists()
    assert not (temp_data_dir / "sessions" / "parent" / "tasks" / f"subagent-{subagent_id}").exists()
    child_state = yaml.safe_load(
        (temp_data_dir / "sessions" / "parent" / "subagents" / subagent_id / "state" / "state.yaml").read_text(encoding="utf-8")
    )
    assert child_state["mode"] == "task"
    assert child_state["plan"]["ready_nodes"] == ["n_accept"]
    parent_graph = list(read_jsonl(store.paths.graph_jsonl))
    assert any(event.get("event") == "subagent_delegated" and event.get("id") == subagent_id for event in parent_graph)
    assert any(event.get("event") == "subagent_finished" and event.get("id") == subagent_id for event in parent_graph)
    sandbox_token = set_runtime_sandbox(sandbox)
    state_token = configure_runtime_task_state(store)
    try:
        wait_result = await subagent_wait.ainvoke({"subagent_id": subagent_id})
        debug = json.loads(await debug_analyze.ainvoke({}))
    finally:
        reset_runtime_sandbox(sandbox_token)
        reset_runtime_task_state(state_token)
    assert "completed" in wait_result
    assert debug["subagents"][0]["child_session_id"] == "parent"
    assert debug["subagents"][0]["child_dag"]["mode"] == "task"
    assert debug["subagents"][0]["child_dag"]["plan"]["node_count"] == 4
    assert debug["subagents"][0]["child_dag"]["plan"]["ready_nodes"] == ["n_accept"]
    assert debug["task"]["thread_id"] == "parent-task"


@pytest.mark.asyncio
async def test_context_tree_tools_use_bound_task_state(temp_data_dir):
    """Agent-facing context tools should operate on the current agent state."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="context-tools",
        session_id="default",
        personality_id="default",
    )
    first = store.record_context_node(turn_id="turn_1", kind="user_message", source="user", payload="first")
    store.record_context_node(turn_id="turn_1", kind="message", source="agent", payload="second")
    token = configure_runtime_task_state(store)
    try:
        before = await context_head.ainvoke({})
        rewind_result = await context_rewind.ainvoke({"node_id": first["node_id"], "reason": "alternate path"})
        after = await context_head.ainvoke({})
    finally:
        reset_runtime_task_state(token)

    assert '"node_count": 2' in before
    assert f"Context head moved to {first['node_id']}" in rewind_result
    assert f'"head": "{first["node_id"]}"' in after


def test_plan_dag_selects_ready_verification_first():
    """The plan graph should be scheduler-readable instead of plain markdown."""
    plan = {
        "version": 1,
        "status": "active",
        "root": "n_goal",
        "nodes": [
            {"id": "n_goal", "type": "goal", "title": "goal", "status": "verified"},
            {"id": "n_write", "type": "subtask", "title": "write", "depends_on": ["n_goal"], "status": "ready"},
            {"id": "n_verify", "type": "verification", "title": "verify", "depends_on": ["n_goal"], "status": "ready"},
        ],
    }

    assert validate_plan(plan) == []
    assert select_ready_node(plan)["id"] == "n_verify"
    state = materialize_plan_state(plan)
    assert state["active_node"] == "n_verify"
    assert state["ready_nodes"] == ["n_write", "n_verify"]


def test_plan_dag_reports_missing_dependencies():
    """Invalid plans should be surfaced in materialized state."""
    plan = {
        "version": 1,
        "status": "active",
        "root": "n_missing",
        "nodes": [
            {"id": "n2", "type": "subtask", "title": "work", "depends_on": ["n1"], "status": "ready"},
        ],
    }

    errors = validate_plan(plan)
    assert "root node is missing: n_missing" in errors
    assert "node n2 depends on missing node n1" in errors
    assert materialize_plan_state(plan)["status"] == "invalid"


def test_completed_plan_nodes_satisfy_dependencies(temp_data_dir):
    """Models often use completed; it should unlock dependent DAG nodes."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="completed-deps",
        session_id="default",
        personality_id="default",
    )
    store.begin_task_mode(
        goal="Accept completed status",
        nodes=[
            {"id": "n001", "title": "Inspect", "depends_on": ["n_goal"], "status": "ready"},
            {"id": "n002", "title": "Implement", "depends_on": ["n001"], "status": "pending"},
        ],
        reason="test",
    )
    store.update_plan_node_status("n001", "completed", reason="inspection done")
    state = store.materialize_state()

    assert state["plan"]["ready_nodes"] == ["n002"]
    assert "n001" in state["plan"]["verified_nodes"]
    assert "n001" in state["plan"]["completed_nodes"]


def test_post_completion_artifacts_attribute_to_last_successful_node(temp_data_dir):
    """Report artifacts created before task_exit should not lose DAG attribution."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="post-completion-artifacts",
        session_id="default",
        personality_id="default",
    )
    store.begin_task_mode(
        goal="Attribute report artifacts",
        nodes=[
            {"id": "n_report", "type": "report", "title": "Report", "depends_on": ["n_goal"], "status": "ready", "priority": 10},
        ],
        reason="test",
    )
    store.update_plan_node_status("n_report", "completed", reason="report done")
    summary = store.record_summary(content="Done.", reason="post report", source="test")

    graph_events = list(read_jsonl(store.paths.graph_jsonl))

    assert summary["plan_node_id"] == "n_report"
    assert any(event.get("id") == summary["summary_id"] and event.get("plan_node_id") == "n_report" for event in graph_events)


def test_task_state_store_versions_plan_updates(temp_data_dir):
    """Plan changes should create a new version and keep prior versions on disk."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="plan-version",
        session_id="default",
        personality_id="default",
    )
    store.begin_task_mode(goal="Version the executable plan", nodes=[], reason="test setup")

    updated = store.add_plan_nodes(
        [
            {
                "id": "n_verify_state",
                "type": "verification",
                "title": "Verify state files",
                "depends_on": ["n_goal"],
                "status": "ready",
            }
        ],
        reason="add verification node",
    )
    state = yaml.safe_load(store.paths.state_yaml.read_text(encoding="utf-8"))
    events = list(read_jsonl(store.paths.events_jsonl))
    version_index = yaml.safe_load((store.paths.plan_versions_dir / "index.yaml").read_text(encoding="utf-8"))
    version_entries = version_index["versions"]

    assert updated["version"] == 2
    assert store.paths.plan_versions_dir == store.paths.versions_dir / "plans"
    assert (store.paths.plan_versions_dir / "latest.yaml").exists()
    assert len(version_entries) >= 3
    assert {entry["version"] for entry in version_entries} >= {1, 2}
    assert all((store.paths.plan_versions_dir / entry["path"]).exists() for entry in version_entries)
    assert state["plan"]["active_node"] == "n_verify_state"
    assert state["plan"]["ready_nodes"] == ["n_verify_state"]
    assert any(event.get("type") == "plan_updated" for event in events)


def test_verify_task_state_checks_materialized_counts(temp_data_dir):
    """Runtime verification should prove file state and append-only logs agree."""
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="verify-state",
        session_id="default",
        personality_id="default",
    )
    store.record_turn_started(turn_id="turn_000001", input_kind="user_message", content="hello")
    store.record_turn_finished(turn_id="turn_000001", status="completed")

    checks = verify_task_state(store)

    assert verification_passed(checks)
    assert {check.name for check in checks} >= {
        "task_files_exist",
        "plan_is_valid_dag",
        "event_count_matches_state",
        "graph_event_count_matches_state",
        "context_tree_event_count_matches_state",
        "context_tree_is_valid",
        "mailbox_event_count_matches_state",
        "mailbox_is_valid",
        "plan_projection_has_no_errors",
        "summaries_are_structured",
    }


def test_tool_result_cache_can_read_persisted_results(temp_data_dir):
    """Large tool results should survive cache object replacement when file-backed."""
    cache_dir = temp_data_dir / "sessions" / "default" / "cache" / "tool-results"
    cache = ToolResultCache(max_inline_chars=10, persist_dir=cache_dir)
    response = cache.maybe_cache("alpha\nbeta\ngamma\n")
    ref = next(line.removeprefix("ref: ") for line in response.splitlines() if line.startswith("ref: "))

    reloaded = ToolResultCache(max_inline_chars=10, persist_dir=cache_dir)

    assert reloaded.read(ref, query="beta") == "beta"
    assert list(cache_dir.glob("*.txt"))
    assert list(cache_dir.glob("*.json"))


@pytest.mark.asyncio
async def test_interaction_records_file_state_events(mock_llm, user_context, temp_data_dir):
    """HermesInteraction should persist user-visible runtime events to agent state."""
    from xbot.interaction import HermesInteraction
    from xbot.graph import build_agent_graph

    mock_llm.set_response_sequence([{"content": "persisted answer"}])
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="interaction-state",
        session_id="default",
        personality_id="default",
    )
    graph = build_agent_graph(
        llm=mock_llm,
        tools=[],
        checkpointer=MemorySaver(),
        store=None,
        permission_system=PermissionSystem(PermissionConfig(default="allow")),
        sandbox_policy=SandboxPolicy(SandboxConfig(enabled=False)),
        hooks=make_default_hooks(),
        tool_registry=make_default_registry(),
    )
    runtime = HermesInteraction(
        user_context=user_context,
        agent_config=type("AgentCfg", (), {"name": "test", "max_context_tokens": 8000})(),
        provider_config=type("ProviderCfg", (), {"name": "mock", "model": "mock"})(),
        graph=graph,
        graph_config={"configurable": {"thread_id": "interaction_state_test"}},
        sandbox=SandboxPolicy(SandboxConfig(enabled=False)),
        tools=[],
        database_path=":memory:",
        state_store=store,
        trace_events=True,
    )

    result = await runtime.send_user_message("hello")

    assert any(getattr(event.payload, "content", None) == "persisted answer" for event in result.events)
    state = yaml.safe_load(store.paths.state_yaml.read_text(encoding="utf-8"))
    events = list(read_jsonl(store.paths.events_jsonl))
    serialized_messages = [
        event["payload"]
        for event in events
        if event.get("type") == "interaction_event" and event.get("kind") == "message"
    ]

    assert state["turn_count"] == 1
    assert state["interaction_event_counts"]["by_kind"]["message"] >= 1
    assert any(message.get("content") == "persisted answer" for message in serialized_messages)


@pytest.mark.asyncio
async def test_interaction_trace_events_are_disabled_by_default(mock_llm, user_context, temp_data_dir):
    """Detailed InteractionEvent traces should not be written unless explicitly enabled."""
    from xbot.interaction import HermesInteraction
    from xbot.graph import build_agent_graph

    mock_llm.set_response_sequence([{"content": "transient answer"}])
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="interaction-no-trace",
        session_id="default",
        personality_id="default",
    )
    graph = build_agent_graph(
        llm=mock_llm,
        tools=[],
        checkpointer=MemorySaver(),
        store=None,
        permission_system=PermissionSystem(PermissionConfig(default="allow")),
        sandbox_policy=SandboxPolicy(SandboxConfig(enabled=False)),
        hooks=make_default_hooks(),
        tool_registry=make_default_registry(),
    )
    runtime = HermesInteraction(
        user_context=user_context,
        agent_config=type("AgentCfg", (), {"name": "test", "max_context_tokens": 8000})(),
        provider_config=type("ProviderCfg", (), {"name": "mock", "model": "mock"})(),
        graph=graph,
        graph_config={"configurable": {"thread_id": "interaction_no_trace_test"}},
        sandbox=SandboxPolicy(SandboxConfig(enabled=False)),
        tools=[],
        database_path=":memory:",
        state_store=store,
    )

    result = await runtime.send_user_message("hello")
    events = list(read_jsonl(store.paths.events_jsonl))
    state = yaml.safe_load(store.paths.state_yaml.read_text(encoding="utf-8"))

    assert any(getattr(event.payload, "content", None) == "transient answer" for event in result.events)
    assert [event.get("type") for event in events] == ["turn_started", "turn_finished"]
    assert state["interaction_event_counts"]["by_kind"] == {}


def test_protocol_encoder_does_not_duplicate_tool_call_blocks():
    """Providers may expose tool calls in both content_blocks and tool_calls."""
    from xbot.interaction import InteractionEvent
    from xbot.protocol import ProtocolEncoder

    message = AIMessage(
        content=[{"type": "tool_call", "name": "shell", "args": {"command": "pwd"}, "id": "call_1"}],
        tool_calls=[{"name": "shell", "args": {"command": "pwd"}, "id": "call_1", "type": "tool_call"}],
    )
    encoder = ProtocolEncoder(session_id="s", thread_id="t")

    frames = encoder.encode_interaction_event(InteractionEvent("message", "agent", message), request_id="req")

    assert [frame.type for frame in frames].count("tool.call.started") == 1


def test_protocol_terminal_renders_tool_call_list_payload(capsys):
    """Terminal renders protocol tool events, including list payloads from runtime normalization."""
    from xbot.interaction import InteractionEvent
    from xbot.protocol import ProtocolEncoder
    from xbot.terminal import ProtocolTerminalRenderer, TerminalOptions

    encoder = ProtocolEncoder(session_id="s", thread_id="t")
    frames = encoder.encode_interaction_event(
        InteractionEvent(
            "tool_call",
            "agent",
            [
                {"name": "shell", "args": {"command": "pwd"}, "id": "call_1"},
                {"name": "filesystem_list", "args": {"path": "."}, "id": "call_2"},
            ],
        ),
        request_id="req",
    )
    renderer = ProtocolTerminalRenderer(agent_name="default", options=TerminalOptions(print_tools=True))

    for frame in frames:
        renderer.frame(frame)

    output = capsys.readouterr().out
    assert output.count("Tool Call>") == 2
    assert "shell(pwd)" in output


def test_protocol_terminal_renders_shell_exec_lifecycle(capsys):
    """Shell/exec results render from protocol lifecycle events, not ToolMessage parsing in the UI."""
    from xbot.interaction import InteractionEvent
    from xbot.protocol import ProtocolEncoder
    from xbot.terminal import ProtocolTerminalRenderer, TerminalOptions

    encoder = ProtocolEncoder(session_id="s", thread_id="t")
    message = ToolMessage(
        content=json.dumps({"stdout": "/tmp/workspace\n", "stderr": "", "exit_code": 0}),
        name="shell",
        tool_call_id="call_1",
    )
    frames = encoder.encode_interaction_event(InteractionEvent("message", "tool", message), request_id="req")
    renderer = ProtocolTerminalRenderer(agent_name="default", options=TerminalOptions(print_tools=True))

    for frame in frames:
        renderer.frame(frame)

    output = capsys.readouterr().out
    assert "Tool shell> running" in output
    assert "Tool shell> exit_code=0" in output
    assert "/tmp/workspace" in output


def test_cache_result_includes_summary_preview_and_metadata(tmp_path):
    """Large cached results should expose more than a cache ref."""
    cache = ToolResultCache(max_inline_chars=8, persist_dir=tmp_path)

    content = "alpha\n" * 20
    cached = cache.maybe_cache(content)

    assert "ref: cache://tool-result/" in cached
    assert "summary:" in cached
    assert "preview:" in cached
    assert "metadata:" in cached
    ref = next(line.removeprefix("ref: ").strip() for line in cached.splitlines() if line.startswith("ref: "))
    metadata = cache.metadata(ref)
    assert metadata["found"] is True
    assert metadata["line_count"] == 20
    assert metadata["preview"]


def test_protocol_encoder_extracts_cache_metadata_from_tool_result():
    """Protocol tool result payloads should carry cache summary and metadata."""
    from xbot.interaction import InteractionEvent
    from xbot.protocol import ProtocolEncoder

    content = "\n".join(
        [
            "Tool result cached.",
            "ref: cache://tool-result/abc",
            "summary: 100 chars, 2 lines. First line: alpha",
            "preview: alpha beta",
            'metadata: {"found": true, "line_count": 2, "preview": "alpha beta", "ref": "cache://tool-result/abc", "size": 100}',
            "read_hint: Use cache_read.",
        ]
    )
    message = ToolMessage(content=content, name="filesystem_read", tool_call_id="call_cache")
    encoder = ProtocolEncoder(session_id="s", thread_id="t")

    frames = encoder.encode_interaction_event(InteractionEvent("message", "tool", message), request_id="req")
    completed = [frame for frame in frames if frame.type == "tool.result.completed"][-1]

    assert completed.payload["result_ref"] == "cache://tool-result/abc"
    assert completed.payload["summary"].startswith("100 chars")
    assert completed.payload["preview"] == "alpha beta"
    assert completed.payload["metadata"]["line_count"] == 2


def test_protocol_encoder_emits_accumulated_usage():
    """Usage is a first-class protocol event with running totals."""
    from xbot.interaction import InteractionEvent
    from xbot.protocol import ProtocolEncoder

    encoder = ProtocolEncoder(session_id="s", thread_id="t")

    first = encoder.encode_interaction_event(InteractionEvent("usage", "runtime", {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}), request_id="req")
    second = encoder.encode_interaction_event(InteractionEvent("usage", "runtime", {"input_tokens": 5, "output_tokens": 6, "total_tokens": 11}), request_id="req")

    assert first[0].type == "usage.updated"
    assert first[0].payload["total"]["total_tokens"] == 7
    assert second[0].payload["total"]["input_tokens"] == 8
    assert second[0].payload["total"]["output_tokens"] == 10
    assert second[0].payload["total"]["total_tokens"] == 18


def test_tui_state_replays_protocol_frames_for_messages_tools_and_interrupts():
    """The TUI state is derived only from protocol frames."""
    from xbot.protocol import ProtocolFrame
    from xbot.tui import TuiState

    state = TuiState(session_id="s", thread_id="t")
    frames = [
        ProtocolFrame(seq=1, direction="server_to_client", type="session.opened", session_id="s", thread_id="t", request_id="open", payload={"agent_name": "default"}),
        ProtocolFrame(seq=2, direction="server_to_client", type="session.ready", session_id="s", thread_id="t", request_id="open", payload={}),
        ProtocolFrame(seq=3, direction="server_to_client", type="message.delta", session_id="s", thread_id="t", request_id="req", payload={"message_id": "m1", "role": "assistant", "content_delta": "hello", "is_reasoning": False}),
        ProtocolFrame(seq=4, direction="server_to_client", type="message.completed", session_id="s", thread_id="t", request_id="req", payload={"message_id": "m1", "role": "assistant", "content": "hello world"}),
        ProtocolFrame(seq=5, direction="server_to_client", type="tool.call.started", session_id="s", thread_id="t", request_id="req", payload={"tool_call_id": "call_1", "name": "shell", "args_preview": "pwd", "status": "pending"}),
        ProtocolFrame(seq=6, direction="server_to_client", type="tool.execution.started", session_id="s", thread_id="t", request_id="req", payload={"tool_call_id": "call_1", "name": "shell"}),
        ProtocolFrame(seq=7, direction="server_to_client", type="tool.result.completed", session_id="s", thread_id="t", request_id="req", payload={"tool_call_id": "call_1", "name": "shell", "exit_code": 0, "result_ref": "cache://tool-result/abc", "summary": "cached shell result"}),
        ProtocolFrame(seq=8, direction="server_to_client", type="usage.updated", session_id="s", thread_id="t", request_id="req", payload={"total": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "requests": 1}}),
        ProtocolFrame(seq=9, direction="server_to_client", type="interrupt.requested", session_id="s", thread_id="t", request_id="req", payload={"interrupt_id": "intr_1", "type": "tool_confirm", "question": "Allow?"}),
    ]

    for frame in frames:
        state.apply(frame)

    assert state.agent_name == "default"
    assert state.messages[-1].content == "hello world"
    assert state._open_stream == {}
    assert state.tools["call_1"].status == "completed"
    assert state.tools["call_1"].result_ref == "cache://tool-result/abc"
    assert state.tools["call_1"].summary == "cached shell result"
    assert state.usage["total_tokens"] == 15
    assert state.pending_interrupt["interrupt_id"] == "intr_1"
    rendered = "\n".join(state.lines(width=80, height=16))
    assert "default> hello world" in rendered
    assert "Tool shell [completed]" in rendered
    assert "cached shell result" in rendered
    assert "Interrupt" in rendered
    assert "total:15" in rendered


def test_tui_client_drains_background_frames_without_blocking():
    """The curses TUI can apply frames delivered by its reader queue."""
    from xbot.protocol import ProtocolFrame
    from xbot.terminal import TerminalOptions
    from xbot.tui import CursesTuiClient

    client = CursesTuiClient(TerminalOptions())
    client._frames.put(
        ProtocolFrame(
            seq=1,
            direction="server_to_client",
            type="message.completed",
            session_id="default",
            thread_id="default",
            request_id="req",
            payload={"message_id": "m", "role": "assistant", "content": "live"},
        )
    )

    client._drain_frames()

    assert client.state.messages[-1].content == "live"


@pytest.mark.asyncio
async def test_runtime_server_jsonl_handshake_and_session_open(temp_data_dir):
    """Runtime server owns HermesInteraction behind protocol frames."""
    from xbot.protocol import ProtocolFrame
    from xbot.server import RuntimeServer

    write_local_runtime(temp_data_dir)
    server = RuntimeServer(data_dir=temp_data_dir)
    hello = ProtocolFrame(
        seq=1,
        direction="client_to_server",
        type="hello",
        session_id="proto",
        thread_id="proto",
        request_id="req_hello",
        payload={"client_name": "test"},
    )
    opened = ProtocolFrame(
        seq=2,
        direction="client_to_server",
        type="session.open",
        session_id="proto",
        thread_id="proto",
        request_id="req_open",
        payload={"personality_id": "default", "streaming": True},
    )

    hello_responses = await server.handle(hello)
    open_responses = await server.handle(opened)

    assert hello_responses[-1].type == "hello.ok"
    assert any(frame.type == "session.opened" for frame in open_responses)
    assert open_responses[-1].type == "session.ready"
    assert server.runtime is not None
    assert server.runtime.state_store.paths.root == temp_data_dir / "sessions" / "proto" / "state"


@pytest.mark.asyncio
async def test_runtime_server_stream_handle_yields_frames_incrementally(temp_data_dir):
    """stream_handle should expose protocol frames as an async stream for live UIs."""
    from xbot.protocol import ProtocolFrame
    from xbot.server import RuntimeServer

    write_local_runtime(temp_data_dir)
    server = RuntimeServer(data_dir=temp_data_dir)
    hello = ProtocolFrame(seq=1, direction="client_to_server", type="hello", session_id="proto", thread_id="proto", request_id="req_hello", payload={})

    seen = []
    async for frame in server.stream_handle(hello):
        seen.append(frame.type)

    assert seen == ["hello.ok"]


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
        hooks=make_default_hooks(),
        tool_registry=make_default_registry(),
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
        hooks=make_default_hooks(),
        tool_registry=make_default_registry(),
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
async def test_interaction_stream_trace_records_events_at_live_plan_node(mock_llm, user_context, temp_data_dir):
    """Streaming trace should persist tool calls while the matching DAG node is active."""
    from xbot.interaction import HermesInteraction
    from xbot.graph import build_agent_graph
    from langgraph.checkpoint.memory import MemorySaver

    mock_llm.set_response_sequence(
        [
            {
                "content": "begin",
                "tool_calls": [{"name": "task_begin", "args": {"goal": "Trace DAG", "steps_json": '["Inspect"]'}, "id": "call_begin"}],
            },
            {
                "content": "next",
                "tool_calls": [{"name": "plan_next", "args": {"reason": "inspect"}, "id": "call_next"}],
            },
            {"content": "done"},
        ]
    )
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "tasks",
        thread_id="stream-live-trace",
        session_id="default",
        personality_id="default",
    )
    graph = build_agent_graph(
        llm=mock_llm,
        tools=[task_begin, plan_next],
        checkpointer=MemorySaver(),
        store=None,
        permission_system=PermissionSystem(PermissionConfig(default="allow")),
        sandbox_policy=SandboxPolicy(SandboxConfig(enabled=False)),
        hooks=make_default_hooks(),
        tool_registry=make_default_registry(),
    )
    runtime = HermesInteraction(
        user_context=user_context,
        agent_config=type("AgentCfg", (), {"name": "test", "max_context_tokens": 8000})(),
        provider_config=type("ProviderCfg", (), {"name": "mock", "model": "mock"})(),
        graph=graph,
        graph_config={"configurable": {"thread_id": "stream_live_trace"}},
        sandbox=SandboxPolicy(SandboxConfig(enabled=False)),
        tools=[task_begin, plan_next],
        database_path=":memory:",
        state_store=store,
        trace_events=True,
    )

    events = [event async for event in runtime.stream_user_message("start")]
    graph_events = list(read_jsonl(store.paths.graph_jsonl))
    persisted_events = list(read_jsonl(store.paths.events_jsonl))

    assert any(event.kind == "tool_call" for event in events)
    assert any(
        event.get("event") == "tool_call_observed"
        and event.get("payload", {}).get("name") == "plan_next"
        and event.get("plan_node_id") == "n001"
        for event in graph_events
    )
    assert not any(event.get("kind") == "message_delta" for event in persisted_events)


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
        hooks=make_default_hooks(),
        tool_registry=make_default_registry(),
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
        hooks=make_default_hooks(),
        tool_registry=make_default_registry(),
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
        hooks=make_default_hooks(),
        tool_registry=make_default_registry(),
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
    from xbot.compaction import split_for_compaction

    ai = AIMessage(content="", tool_calls=[{"name": "shell", "args": {"command": "pwd"}, "id": "call_1", "type": "tool_call"}])
    tool_result = ToolMessage(content="ok", name="shell", tool_call_id="call_1")

    to_compress, keep = split_for_compaction([HumanMessage(content="hi"), ai, tool_result], keep_recent_messages=1)

    assert ai in keep
    assert tool_result in keep
    assert all(message not in to_compress for message in [ai, tool_result])


def test_split_for_compaction_keeps_unresolved_tool_calls():
    """Compaction must not hide unresolved assistant tool calls."""
    from xbot.compaction import split_for_compaction

    old = HumanMessage(content="old")
    ai = AIMessage(content="", tool_calls=[{"name": "shell", "args": {"command": "pwd"}, "id": "call_1", "type": "tool_call"}])
    recent = HumanMessage(content="recent")

    to_compress, keep = split_for_compaction([old, ai, recent], keep_recent_messages=1)

    assert old in to_compress
    assert ai in keep
    assert recent in keep


@pytest.mark.asyncio
async def test_prepare_context_records_auditable_compaction_state(mock_llm, temp_data_dir):
    """Compaction should create durable summary, graph, and context-tree audit records."""
    from xbot.graph import make_prepare_context_node

    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "state",
        thread_id="compact-audit",
        session_id="compact-audit",
        personality_id="default",
        task_id="agent",
        direct_root=True,
        goal="# Goal\n\nAudit compaction.\n",
    )
    mock_llm.set_response_sequence([{"content": "summary with source markers"}])
    node = make_prepare_context_node(
        mock_llm,
        max_messages_before_compress=1,
        max_context_chars=10_000,
        keep_recent_messages=1,
    )
    messages = [
        HumanMessage(content="first", id="m_first"),
        AIMessage(content="second", id="m_second"),
        HumanMessage(content="recent", id="m_recent"),
    ]

    token = configure_runtime_task_state(store)
    try:
        result = await node({"messages": messages})
    finally:
        reset_runtime_task_state(token)

    assert result["compression_requested"] is False
    assert result["messages"][1].additional_kwargs["summary_of"] == ["m_first", "m_second"]
    summary = store.latest_summaries(limit=1)[0]
    assert summary["source_message_refs"] == ["m_first", "m_second"]
    assert summary["source_range"] == "m_first..m_second"
    graph_events = list(read_jsonl(store.paths.graph_jsonl))
    assert any(event.get("event") == "context_compacted" and event.get("summary_id") == summary["summary_id"] for event in graph_events)
    context_events = list(read_jsonl(store.paths.context_tree_jsonl))
    assert any(event.get("kind") == "context_compacted" and event.get("payload", {}).get("summary_id") == summary["summary_id"] for event in context_events)


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
    runtime._normalizer._seen_message_keys.add("x")
    runtime._normalizer._streamed_message_keys.add("x")
    runtime._normalizer._streamed_tool_call_keys.add("x")
    runtime._normalizer._seen_runtime_event_keys.add("x")

    runtime.reset_thread("clean")

    assert runtime.graph_config == {"configurable": {"thread_id": "clean"}}
    assert not runtime._normalizer._seen_message_keys
    assert not runtime._normalizer._streamed_message_keys
    assert not runtime._normalizer._streamed_tool_call_keys
    assert not runtime._normalizer._seen_runtime_event_keys


@pytest.mark.asyncio
async def test_disabled_sandbox_shell_does_not_mock_success():
    """Disabling sandbox must not make shell appear to execute successfully."""
    policy = SandboxPolicy(SandboxConfig(enabled=False))

    with pytest.raises(RuntimeError, match="requires the system sandbox"):
        await policy.run_shell("pwd")


@pytest.mark.asyncio
async def test_tool_batch_uses_sequential_failure_barrier(mock_llm, user_context, temp_data_dir):
    """Sequential state tools should stop later same-batch side effects on failure."""
    from xbot.graph import build_agent_graph

    workspace = temp_data_dir / "sessions" / "default" / "workspace"
    target = workspace / "should_not_exist.txt"
    store = TaskStateStore.create(
        tasks_root=temp_data_dir / "sessions" / "default" / "state",
        thread_id="tool-batch-barrier",
        session_id="default",
        personality_id="default",
        direct_root=True,
    )
    token = configure_runtime_task_state(store)
    try:
        await task_begin.ainvoke({"goal": "already active"})
        mock_llm.set_response_sequence(
            [
                {
                    "content": "",
                    "tool_calls": [
                        {"name": "task_begin", "args": {"goal": "new task"}, "id": "call_begin"},
                        {"name": "filesystem_write", "args": {"path": "should_not_exist.txt", "content": "bad"}, "id": "call_write"},
                    ],
                },
                {"content": "done"},
            ]
        )
        graph = build_agent_graph(
            llm=mock_llm,
            tools=[task_begin, filesystem_write],
            checkpointer=MemorySaver(),
            store=None,
            permission_system=PermissionSystem(PermissionConfig(default="allow")),
            sandbox_policy=SandboxPolicy(SandboxConfig(enabled=False), data_root=temp_data_dir, workspace_root=workspace),
            hooks=make_default_hooks(),
            tool_registry=make_default_registry(),
        )
        result = await graph.ainvoke(
            {
                "messages": [HumanMessage(content="try batch")],
                "user_context": user_context,
            },
            config={"configurable": {"thread_id": "tool-batch-barrier"}},
        )
    finally:
        reset_runtime_task_state(token)

    tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert tool_messages[0].name == "task_begin"
    assert tool_messages[0].status == "error"
    assert tool_messages[1].name == "filesystem_write"
    assert tool_messages[1].status == "error"
    assert "is a sequential barrier" in str(tool_messages[1].content)
    assert not target.exists()


@pytest.mark.asyncio
async def test_parallel_filesystem_writes_run_in_same_batch(mock_llm, user_context, temp_data_dir):
    """Parallel-safe filesystem writes should execute in one ToolNode batch with path locks."""
    from xbot.graph import build_agent_graph

    workspace = temp_data_dir / "sessions" / "default" / "workspace"
    sandbox = SandboxPolicy(SandboxConfig(enabled=False), data_root=temp_data_dir, workspace_root=workspace)
    mock_llm.set_response_sequence(
        [
            {
                "content": "",
                "tool_calls": [
                    {"name": "filesystem_write", "args": {"path": "a.txt", "content": "a"}, "id": "call_a"},
                    {"name": "filesystem_write", "args": {"path": "b.txt", "content": "b"}, "id": "call_b"},
                ],
            },
            {"content": "done"},
        ]
    )
    graph = build_agent_graph(
        llm=mock_llm,
        tools=[filesystem_write],
        checkpointer=MemorySaver(),
        store=None,
        permission_system=PermissionSystem(PermissionConfig(default="allow")),
        sandbox_policy=sandbox,
        hooks=make_default_hooks(),
        tool_registry=make_default_registry(),
    )
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="write two files")],
            "user_context": user_context,
        },
        config={"configurable": {"thread_id": "parallel-filesystem-writes"}},
    )

    tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert [message.name for message in tool_messages] == ["filesystem_write", "filesystem_write"]
    assert all(getattr(message, "status", "success") != "error" for message in tool_messages)
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "a"
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "b"


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
        hooks=make_default_hooks(),
        tool_registry=make_default_registry(),
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
        hooks=make_default_hooks(),
        tool_registry=make_default_registry(),
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
        hooks=make_default_hooks(),
        tool_registry=make_default_registry(),
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


# ============================================================================
# LoopHooks tests
# ============================================================================


class TestLoopHooks:
    """Tests for xbot.hooks.LoopHooks."""

    def test_hooks_run_in_registration_order(self):
        from xbot.hooks import LoopHooks
        import asyncio

        hooks = LoopHooks()
        order: list[str] = []

        async def hook_a(ctx):
            order.append("a")
            return None

        async def hook_b(ctx):
            order.append("b")
            return None

        hooks.register("before_agent", hook_a)
        hooks.register("before_agent", hook_b)

        result = asyncio.run(hooks.run("before_agent", {}))
        assert result is None
        assert order == ["a", "b"]

    def test_hook_short_circuit_returns_first_truthy(self):
        from xbot.hooks import LoopHooks
        import asyncio

        hooks = LoopHooks()
        order: list[str] = []

        async def hook_a(ctx):
            order.append("a")
            return {"short": "circuit"}

        async def hook_b(ctx):
            order.append("b")
            return None

        hooks.register("before_agent", hook_a)
        hooks.register("before_agent", hook_b)

        result = asyncio.run(hooks.run("before_agent", {}))
        assert result == {"short": "circuit"}
        assert order == ["a"]  # hook_b never ran

    def test_unknown_stage_raises(self):
        from xbot.hooks import LoopHooks

        hooks = LoopHooks()
        with pytest.raises(ValueError, match="Unknown hook stage"):
            hooks.register("nonexistent_stage", lambda ctx: None)

    def test_all_stages_are_registrable(self):
        from xbot.hooks import LoopHooks

        hooks = LoopHooks()
        for stage in LoopHooks.STAGES:
            async def noop(ctx):
                return None
            hooks.register(stage, noop)
            assert len(getattr(hooks, stage)) == 1

    def test_build_default_hooks_returns_empty_hooks(self):
        from xbot.hooks import build_default_hooks

        hooks = build_default_hooks()
        for stage in hooks.STAGES:
            assert getattr(hooks, stage) == []

    def test_load_standard_hooks_registers_configured_hooks_after_standard_hooks(self, monkeypatch):
        from types import ModuleType
        import asyncio
        import sys

        from xbot.hooks import load_standard_hooks

        module = ModuleType("xbot_test_configured_hooks")

        async def configured_hook(ctx):
            ctx.setdefault("order", []).append("configured")
            return None

        module.configured_hook = configured_hook
        monkeypatch.setitem(sys.modules, "xbot_test_configured_hooks", module)

        hooks = load_standard_hooks(
            [{"stage": "before_tools", "target": "xbot_test_configured_hooks:configured_hook"}]
        )
        ctx = {
            "tool_calls": [],
            "order": [],
            "permission_system": None,
            "sandbox_policy": None,
            "tool_registry": None,
        }

        result = asyncio.run(hooks.run("before_tools", ctx))

        assert result is None
        assert ctx["order"] == ["configured"]
        assert len(hooks.before_tools) >= 3

    def test_configured_hook_target_must_be_importable(self):
        from xbot.hooks import load_standard_hooks

        with pytest.raises(ModuleNotFoundError):
            load_standard_hooks([{"stage": "before_agent", "target": "missing_hook_module:hook"}])

    def test_configured_hook_target_must_be_async(self, monkeypatch):
        from types import ModuleType
        import sys

        from xbot.hooks import load_standard_hooks

        module = ModuleType("xbot_test_sync_hooks")

        def sync_hook(ctx):
            return None

        module.sync_hook = sync_hook
        monkeypatch.setitem(sys.modules, "xbot_test_sync_hooks", module)

        with pytest.raises(ValueError, match="must be async"):
            load_standard_hooks([{"stage": "before_agent", "target": "xbot_test_sync_hooks:sync_hook"}])


# ============================================================================
# ToolRegistry tests
# ============================================================================


class TestToolRegistry:
    """Tests for xbot.registry.ToolRegistry."""

    def test_register_and_get_tool(self):
        from xbot.registry import ToolRegistry
        from langchain_core.tools import tool as lc_tool

        @lc_tool
        def test_tool(x: str) -> str:
            """A test tool."""
            return x

        registry = ToolRegistry()
        registry.register(test_tool, sandbox_mode="sandboxed", execution_mode="parallel")

        assert registry.get("test_tool") is test_tool
        assert registry.sandbox_mode("test_tool") == "sandboxed"
        assert registry.execution_mode("test_tool") == "parallel"
        assert len(registry) == 1
        assert "test_tool" in registry

    def test_unregistered_tool_returns_none(self):
        from xbot.registry import ToolRegistry

        registry = ToolRegistry()
        assert registry.get("nonexistent") is None
        assert registry.sandbox_mode("nonexistent") == "unregistered"
        assert registry.execution_mode("nonexistent") == "unregistered"
        assert not registry.registered("nonexistent")

    def test_filter_expands_filesystem_wildcard(self):
        from xbot.registry import ToolRegistry
        from langchain_core.tools import tool as lc_tool

        @lc_tool
        def filesystem_read(path: str) -> str:
            """Read a file."""
            return path

        @lc_tool
        def filesystem_write(path: str, content: str) -> str:
            """Write a file."""
            return path

        @lc_tool
        def filesystem_list(path: str) -> str:
            """List a directory."""
            return path

        @lc_tool
        def other_tool(x: str) -> str:
            """Other tool."""
            return x

        registry = ToolRegistry()
        registry.register(filesystem_read, sandbox_mode="sandboxed", execution_mode="parallel")
        registry.register(filesystem_write, sandbox_mode="sandboxed", execution_mode="parallel", lock_fields=("path",))
        registry.register(filesystem_list, sandbox_mode="sandboxed", execution_mode="parallel")
        registry.register(other_tool, sandbox_mode="host", execution_mode="sequential")

        result = registry.filter(["filesystem"])
        assert len(result) == 3
        names = [t.name for t in result]
        assert "filesystem_read" in names
        assert "filesystem_write" in names
        assert "filesystem_list" in names

    def test_filter_preserves_order(self):
        from xbot.registry import ToolRegistry
        from langchain_core.tools import tool as lc_tool

        @lc_tool
        def tool_a(x: str) -> str:
            """A."""
            return x

        @lc_tool
        def tool_b(x: str) -> str:
            """B."""
            return x

        @lc_tool
        def tool_c(x: str) -> str:
            """C."""
            return x

        registry = ToolRegistry()
        registry.register(tool_a, sandbox_mode="host", execution_mode="parallel")
        registry.register(tool_b, sandbox_mode="host", execution_mode="parallel")
        registry.register(tool_c, sandbox_mode="host", execution_mode="parallel")

        result = registry.filter(["tool_c", "tool_a"])
        assert [t.name for t in result] == ["tool_c", "tool_a"]

    def test_unregister_removes_tool(self):
        from xbot.registry import ToolRegistry
        from langchain_core.tools import tool as lc_tool

        @lc_tool
        def temp_tool(x: str) -> str:
            """Temp."""
            return x

        registry = ToolRegistry()
        registry.register(temp_tool, sandbox_mode="host", execution_mode="sequential")
        assert len(registry) == 1

        registry.unregister("temp_tool")
        assert len(registry) == 0
        assert registry.get("temp_tool") is None

    def test_registry_metadata_returns_all_modes(self):
        from xbot.registry import ToolRegistry
        from langchain_core.tools import tool as lc_tool

        @lc_tool
        def sandboxed_tool(x: str) -> str:
            """Sandboxed tool."""
            return x

        @lc_tool
        def host_tool(x: str) -> str:
            """Host tool."""
            return x

        registry = ToolRegistry()
        registry.register(sandboxed_tool, sandbox_mode="sandboxed", execution_mode="parallel", lock_fields=("path",))
        registry.register(host_tool, sandbox_mode="host", execution_mode="sequential")

        assert registry.sandbox_modes() == {"sandboxed_tool": "sandboxed", "host_tool": "host"}
        assert registry.execution_modes() == {"sandboxed_tool": "parallel", "host_tool": "sequential"}
        assert registry.lock_fields("sandboxed_tool") == ("path",)

    def test_register_many_requires_sandbox_metadata(self):
        from xbot.registry import ToolRegistry
        from langchain_core.tools import tool as lc_tool

        @lc_tool
        def tool_without_metadata(x: str) -> str:
            """A tool."""
            return x

        registry = ToolRegistry()
        with pytest.raises(ValueError, match="missing sandbox metadata"):
            registry.register_many([tool_without_metadata], sandbox_modes={}, execution_modes={})

    def test_register_many_requires_execution_metadata(self):
        from xbot.registry import ToolRegistry
        from langchain_core.tools import tool as lc_tool

        @lc_tool
        def tool_without_execution_metadata(x: str) -> str:
            """A tool."""
            return x

        registry = ToolRegistry()
        with pytest.raises(ValueError, match="missing execution metadata"):
            registry.register_many(
                [tool_without_execution_metadata],
                sandbox_modes={"tool_without_execution_metadata": "host"},
                execution_modes={},
            )

    def test_register_rejects_invalid_sandbox_mode(self):
        from xbot.registry import ToolRegistry
        from langchain_core.tools import tool as lc_tool

        @lc_tool
        def invalid_mode_tool(x: str) -> str:
            """A tool."""
            return x

        registry = ToolRegistry()
        with pytest.raises(ValueError, match="invalid sandbox mode"):
            registry.register(invalid_mode_tool, sandbox_mode="unknown", execution_mode="parallel")  # type: ignore[arg-type]

    def test_register_rejects_invalid_execution_mode(self):
        from xbot.registry import ToolRegistry
        from langchain_core.tools import tool as lc_tool

        @lc_tool
        def invalid_execution_tool(x: str) -> str:
            """A tool."""
            return x

        registry = ToolRegistry()
        with pytest.raises(ValueError, match="invalid execution mode"):
            registry.register(invalid_execution_tool, sandbox_mode="host", execution_mode="serial")  # type: ignore[arg-type]

    def test_bootstrap_registry_loads_all_tools(self):
        from xbot.registry import bootstrap_registry

        registry = bootstrap_registry()
        assert len(registry) == 33
        # Verify key tools are present
        assert registry.registered("shell")
        assert registry.registered("filesystem_read")
        assert registry.registered("task_begin")
        assert registry.registered("task_status")
        assert registry.registered("task_exit")
        assert registry.registered("plan_add_nodes")
        assert registry.registered("plan_next")
        assert registry.registered("debug_analyze")
        # Verify sandbox modes
        assert registry.sandbox_mode("shell") == "sandboxed"
        assert registry.sandbox_mode("debug_analyze") == "host"
        assert registry.execution_mode("filesystem_write") == "parallel"
        assert registry.lock_fields("filesystem_write") == ("path",)
        assert registry.execution_mode("task_begin") == "sequential"
        assert registry.validate_sandbox_modes() == []
        assert registry.validate_execution_modes() == []

    def test_bootstrap_registry_filter_auto_includes_cache_read(self):
        from xbot.registry import bootstrap_registry

        registry = bootstrap_registry()
        result = registry.filter(["shell"])

        assert [tool.name for tool in result] == ["shell", "cache_read"]

    def test_builtin_tools_are_complete_base_tools_with_runtime_metadata(self):
        from langchain_core.tools import BaseTool
        from xbot.builtin_tools import TOOL_EXECUTION_MODE, TOOL_SANDBOX_MODE, get_all_tools

        tools = get_all_tools()
        names = [tool.name for tool in tools]

        assert len(tools) == 33
        assert all(isinstance(tool, BaseTool) for tool in tools)
        assert len(names) == len(set(names))
        assert set(names) == set(TOOL_SANDBOX_MODE)
        assert set(names) == set(TOOL_EXECUTION_MODE)
        assert {"task_begin", "task_status", "task_exit", "plan_add_nodes"} <= set(names)

    def test_core_tool_descriptions_explain_contracts(self):
        from xbot.registry import bootstrap_registry

        registry = bootstrap_registry()
        expected_terms = {
            "task_begin": ["Do not call it again", "Never put a tool-call order"],
            "plan_add_nodes": ["node objects", "dependencies", "not which tool to call"],
            "plan_update": ["durable execution facts", "evidence_refs_json", "completion_errors"],
            "filesystem_write": ["complete content", "Read the file first", "locked"],
            "memory_update": ["durable", "Do not store current task progress"],
            "summary_add": ["cross-turn", "Do not call this for every small tool result"],
            "cache_read": ["cache://", "query", "max_chars"],
        }
        for tool_name, terms in expected_terms.items():
            description = registry.get(tool_name).description or ""
            assert len(description) >= 180
            for term in terms:
                assert term in description

# ============================================================================
# Cache-friendly context tests
# ============================================================================


class TestCacheFriendlyContext:
    """Tests for the refactored xbot.context module."""

    def test_system_prompt_is_memoized(self, temp_data_dir):
        from xbot.config import configure_runtime_paths
        from xbot.context import get_system_prompt, invalidate_system_prompt_cache

        paths = configure_runtime_paths(session_id="ctx-test", personality_id="default", data_dir=temp_data_dir)
        user_ctx = UserContext(user_id="test-user", user_name="Tester")

        invalidate_system_prompt_cache()
        prompt1 = get_system_prompt(user_context=user_ctx, agent_role="tester", sandbox_summary="sandbox: off")
        prompt2 = get_system_prompt(user_context=user_ctx, agent_role="tester", sandbox_summary="sandbox: off")

        assert prompt1 == prompt2
        assert len(prompt1) > 100

        invalidate_system_prompt_cache()
        prompt3 = get_system_prompt(user_context=user_ctx, agent_role="tester", sandbox_summary="sandbox: off", system_notice="changed notice")
        assert prompt3 != prompt1
        assert "changed notice" in prompt3

    def test_build_dag_suffix_contains_task_context(self, temp_data_dir):
        from xbot.context import build_dag_suffix

        suffix = build_dag_suffix(
            sandbox_summary="sandbox: off",
            user_context=UserContext(user_id="test-user", user_name="Tester"),
            task_context="# Task\nactive node: n1",
            pending_mailbox_items=2,
        )
        assert "# Current State" in suffix
        assert "# Task State Projection" in suffix
        assert "active node: n1" in suffix
        assert "pending_mailbox_items: 2" in suffix

    def test_context_messages_requires_explicit_projection(self, temp_data_dir):
        from xbot.context import build_context_messages, invalidate_system_prompt_cache
        from langchain_core.messages import HumanMessage

        invalidate_system_prompt_cache()

        user_ctx = UserContext(user_id="test-user", user_name="Tester")
        state = {
            "user_context": user_ctx.model_dump(),
            "agent_role": "test assistant",
            "active_subagents": [],
            "system_notice": "",
        }

        with pytest.raises(ValueError, match="runtime_frame or context_projection"):
            build_context_messages(
                state,
                sandbox_summary="sandbox: enabled",
                message_chain=[HumanMessage(content="hello")],
            )

    def test_context_messages_use_runtime_frame_projection(self, temp_data_dir):
        from langchain_core.messages import HumanMessage
        from xbot.config import RuntimePaths
        from xbot.context import build_context_messages, invalidate_system_prompt_cache
        from xbot.runtime import (
            PersonalityProjection,
            RuntimeFrame,
            SandboxProjection,
            TaskProjection,
            ToolRegistrySnapshot,
        )

        invalidate_system_prompt_cache()
        user_ctx = UserContext(user_id="frame-user", user_name="Frame User")
        frame = RuntimeFrame(
            runtime=RuntimeContext(
                paths=RuntimePaths(data_dir=temp_data_dir, session_id="frame-session", personality_id="default"),
                thread_id="frame-thread",
                task_id="agent",
                run_id="run_frame",
                trace_id="trace_frame",
            ),
            user=user_ctx,
            personality=PersonalityProjection(
                name="default",
                agent_role="Frame-specific engineer",
                system_template="User: {{ user_context.user_name }}\nRole: {{ agent_config.agent_role }}",
                instructions="Use explicit frame instructions.",
                memory="Frame memory entry.",
                skills_summary="Frame skill summary.",
            ),
            sandbox=SandboxProjection(summary="frame sandbox"),
            tools=ToolRegistrySnapshot(names=("shell",), sandbox_modes={"shell": "sandboxed"}),
            task=TaskProjection(context_text="Frame task projection.", pending_mailbox_items=3),
            system_notice="frame notice",
            active_subagents=("child-a",),
        )

        messages = build_context_messages(
            {"runtime_frame": frame},
            sandbox_summary="ignored sandbox",
            message_chain=[HumanMessage(content="hello")],
        )

        assert "Frame-specific engineer" in messages[0].content
        assert "Use explicit frame instructions." in messages[0].content
        assert "Frame memory entry." in messages[0].content
        assert "Frame skill summary." in messages[0].content
        assert "frame sandbox" in messages[0].content
        assert "Frame task projection." in messages[-1].content
        assert "pending_mailbox_items: 3" in messages[-1].content
        assert "active_subagents: 1" in messages[-1].content
