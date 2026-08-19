import asyncio
import json

import pytest

from XBotv2.core.jobs import JobKind, JobResult
from XBotv2.jobs import JobRegistry
from XBotv2.core.tools import ToolCall
from XBotv2.coretools.shell import SHELL_TOOLS, run_shell_command
from XBotv2.permissions.system import PermissionSystem
from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.agentloop.tool_runtime import execute_tools
from XBotv2.permission_request.service import ApprovalService
from XBotv2.application.client_events import ClientEventRouter
from XBotv2.tests.helpers import make_tool_ctx
import xcore
from XBotv2.sandbox.policy import SandboxPolicy


def make_tools(temp_workspace, *, sandbox=None):
    registry = JobRegistry()
    tools = {tool.name: tool for tool in SHELL_TOOLS}
    return registry, tools


def invoke(tools, name, args, registry, sandbox=None):
    return tools[name].ainvoke(
        args, job_registry=registry, sandbox=sandbox, sandbox_policy=sandbox
    )


def patch_shell_executor(monkeypatch, replacement):
    """Patch the executor used by the already-imported singleton tools."""
    shell_tool = next(tool for tool in SHELL_TOOLS if tool.name == "shell")
    monkeypatch.setitem(
        shell_tool.function.__globals__,
        "run_shell_command",
        replacement,
    )


@pytest.mark.asyncio
async def test_background_shell_lifecycle_and_read(temp_workspace, monkeypatch):
    async def run(*args, **kwargs):
        return "background-output"

    patch_shell_executor(monkeypatch, run)
    registry, tools = make_tools(temp_workspace)
    assert set(tools) == {
        "shell", "list_shells",
        "wait_shell", "read_shell", "cancel_shell",
    }
    assert "background" in tools["shell"].parameters["properties"]

    started = await invoke(
        tools, "shell", {"command": "printf background-output", "background": True}, registry
    )
    # Content is "Started <job_id>" — extract the job ID
    job_id = started.content.split("Started ")[1]
    waited = await invoke(tools, "wait_shell", {"ids": [job_id]}, registry)
    waited_data = json.loads(waited.content)
    assert waited_data["ready"][0]["status"] == "completed"
    assert waited_data["pending"] == []
    assert waited_data["timed_out"] is False
    read = await invoke(tools, "read_shell", {"id": job_id}, registry)
    assert read.content == "background-output"
    listed = await invoke(tools, "list_shells", {}, registry)
    listed_data = json.loads(listed.content)
    assert [item["id"] for item in listed_data["shells"]] == [job_id]


@pytest.mark.asyncio
async def test_foreground_shell_defaults_to_workspace_when_sandbox_is_disabled(
    temp_workspace,
):
    sandbox = SandboxPolicy(enabled=False, workspace_root=temp_workspace)
    registry, tools = make_tools(temp_workspace, sandbox=sandbox)

    result = await invoke(
        tools, "shell", {"command": "pwd", "cwd": str(temp_workspace)}, registry,
        sandbox=sandbox,
    )

    assert result.status == "success"
    assert result.content.strip() == str(temp_workspace)


@pytest.mark.asyncio
async def test_escalated_shell_bypasses_sandbox_in_both_modes(
    temp_workspace,
    monkeypatch,
):
    sandboxes = []

    async def run(*args, sandbox=None, **kwargs):
        sandboxes.append(sandbox)
        return "output"

    patch_shell_executor(monkeypatch, run)
    registry, tools = make_tools(temp_workspace, sandbox=object())

    foreground = await invoke(
        tools, "shell",
        {"command": "install dependency",
         "sandbox_permissions": "require_escalated",
         "justification": "Install a required dependency."},
        registry,
        sandbox=object(),
    )
    background = await invoke(
        tools, "shell",
        {"command": "install dependency",
         "background": True,
         "sandbox_permissions": "require_escalated",
         "justification": "Install a required dependency."},
        registry,
        sandbox=object(),
    )
    await invoke(tools, "wait_shell", {"ids": [background.content.split("Started ")[1]]}, registry)

    assert foreground.status == "success"
    assert foreground.content == "output"
    assert sandboxes == [None, None]


@pytest.mark.asyncio
async def test_snapshot_bounds_output_but_read_keeps_full_content(
    temp_workspace, monkeypatch
):
    full_output = "x" * 13_000

    async def run(*args, **kwargs):
        return full_output

    patch_shell_executor(monkeypatch, run)
    registry, tools = make_tools(temp_workspace)
    started = await invoke(
        tools, "shell", {"command": "generate output", "background": True}, registry
    )
    job = registry.get(started.content.split("Started ")[1])
    await job.runner_task
    await registry.wait([job.id])

    assert len(registry.snapshot(job)["output"]) < 2_100
    read = await invoke(
        tools, "read_shell", {"id": job.id, "max_bytes": 20_000}, registry
    )
    assert read.content == full_output


@pytest.mark.asyncio
async def test_cancel_shell_stops_process(temp_workspace, monkeypatch):
    async def run(*args, **kwargs):
        await asyncio.Event().wait()

    patch_shell_executor(monkeypatch, run)
    registry, tools = make_tools(temp_workspace)
    started = await invoke(tools, "shell", {"command": "sleep 30", "background": True}, registry)
    job = registry.get(started.content.split("Started ")[1])
    while job.status.value != "running":
        await asyncio.sleep(0)

    result = await asyncio.wait_for(
        invoke(tools, "cancel_shell", {"id": job.id}, registry), timeout=1
    )

    assert result.status == "success"
    assert job.status.value == "cancelled"
    assert job.runner_task.done()


@pytest.mark.asyncio
async def test_shutdown_stops_jobs_without_completion_delivery(
    temp_workspace, monkeypatch
):
    async def run(*args, **kwargs):
        await asyncio.Event().wait()

    patch_shell_executor(monkeypatch, run)
    completions = []
    registry, tools = make_tools(temp_workspace)

    async def record_completion(task):
        completions.append(task)

    registry.on_complete = record_completion
    started = await invoke(tools, "shell", {"command": "sleep 30", "background": True}, registry)
    await asyncio.sleep(0)

    await asyncio.wait_for(registry.shutdown(), timeout=1)

    assert registry.get_or_none(started.content.split("Started ")[1]) is None
    assert completions == []


@pytest.mark.asyncio
async def test_wait_shell_returns_exit_code_for_completed_job(
    temp_workspace, monkeypatch
):
    async def run(*args, **kwargs):
        return "ok"

    patch_shell_executor(monkeypatch, run)
    registry, tools = make_tools(temp_workspace)
    started = await invoke(tools, "shell", {"command": "true", "background": True}, registry)
    waited = await invoke(tools, "wait_shell", {"ids": [started.content.split("Started ")[1]]}, registry)
    assert json.loads(waited.content)["ready"][0]["exit_code"] == 0


@pytest.mark.asyncio
async def test_escalated_background_shell_requires_approval(
    temp_workspace, monkeypatch
):
    async def run(*args, **kwargs):
        return "ran"

    patch_shell_executor(monkeypatch, run)
    sandbox = SandboxPolicy(
        {"enabled": True, "external_write": "ask"},
        workspace_root=str(temp_workspace),
    )
    registry = ToolRegistry()
    job_registry = JobRegistry()
    registry.register(
        next(tool for tool in SHELL_TOOLS if tool.name == "shell"),
        injected={"sandbox": sandbox, "job_registry": job_registry},
    )
    events = []

    async def approve(event):
        events.append(event)
        return "allowed-once"

    service_ctx = xcore.Context()
    approval = ApprovalService(service_ctx, ClientEventRouter())
    approval.register_answerer(approve)
    ctx = make_tool_ctx(
        registry,
        sandbox=sandbox,
        permissions=PermissionSystem(default_decision="allow"),
        approval=approval,
        base=service_ctx,
    )
    results = await ctx.tools.execute_all(
        [ToolCall("c1", "shell", {
            "command": "pwd",
            "background": True,
            "sandbox_permissions": "require_escalated",
            "justification": "Need host access.",
        })],
    )

    assert results[0].status == "success"
    assert events and events[0]["data"]["source"] == "permission_system"
    assert "Need host access." in events[0]["data"]["reason"]
    job = job_registry.get("sh_1")
    assert job.metadata["escalated"] is True
    await job.runner_task
    assert job.status.value == "completed"


@pytest.mark.asyncio
async def test_denied_background_shell_escalation_creates_no_job(
    temp_workspace,
):
    sandbox = SandboxPolicy(
        {"enabled": True, "external_write": "ask"},
        workspace_root=str(temp_workspace),
    )
    registry = ToolRegistry()
    job_registry = JobRegistry()
    registry.register(
        next(tool for tool in SHELL_TOOLS if tool.name == "shell"),
        injected={"sandbox": sandbox, "job_registry": job_registry},
    )

    async def deny(event):
        del event
        return "rejected"

    service_ctx = xcore.Context()
    approval = ApprovalService(service_ctx, ClientEventRouter())
    approval.register_answerer(deny)
    ctx = make_tool_ctx(
        registry,
        sandbox=sandbox,
        permissions=PermissionSystem(default_decision="allow"),
        approval=approval,
        base=service_ctx,
    )
    results = await ctx.tools.execute_all(
        [ToolCall("c1", "shell", {
            "command": "pwd",
            "background": True,
            "sandbox_permissions": "require_escalated",
            "justification": "Need host access.",
        })],
    )

    assert results[0].status == "error"
    assert job_registry.all() == []


@pytest.mark.asyncio
async def test_host_shell_cancellation_reaps_process_group(
    temp_workspace, monkeypatch
):
    waiting = asyncio.Event()

    class Process:
        pid = 123
        returncode = None

        def poll(self):
            waiting.set()
            return self.returncode

    process = Process()

    def create_process(*args, **kwargs):
        return process

    def signal_process(proc):
        proc.returncode = -9

    monkeypatch.setattr(
        run_shell_command.__globals__["subprocess"],
        "Popen",
        create_process,
    )
    monkeypatch.setitem(
        run_shell_command.__globals__,
        "_signal_process",
        signal_process,
    )
    command = asyncio.create_task(
        run_shell_command(
            "sleep 30",
            cwd=str(temp_workspace),
            timeout_seconds=0,
        )
    )
    await asyncio.wait_for(waiting.wait(), timeout=1)
    command.cancel()

    with pytest.raises(asyncio.CancelledError):
        await command
    assert process.returncode == -9


@pytest.mark.asyncio
async def test_host_shell_returns_complete_output_for_common_cache(
    temp_workspace, monkeypatch
):
    class Process:
        pid = 123
        returncode = 0

        def poll(self):
            return self.returncode

    def create_process(*args, stdout, **kwargs):
        stdout.write(b"x" * 100_001)
        return Process()

    monkeypatch.setattr(
        run_shell_command.__globals__["subprocess"],
        "Popen",
        create_process,
    )

    result = await run_shell_command("generate", cwd=str(temp_workspace))

    assert result == "x" * 100_001
