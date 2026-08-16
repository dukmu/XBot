import asyncio

import pytest

from XBotv2.jobs import JobKind, JobRegistry, JobResult
from XBotv2.core.tools import ToolCall
from XBotv2.coretools.shell import SHELL_TOOLS, run_shell_command
from XBotv2.permissions.system import PermissionSystem
from XBotv2.tools.registry import ToolRegistry
from XBotv2.tools.runtime import execute_tools
from XBotv2.sandbox.policy import SandboxPolicy


def make_tools(temp_workspace, *, sandbox=None):
    registry = JobRegistry()
    tools = {tool.name: tool for tool in SHELL_TOOLS}
    return registry, tools


def invoke(tools, name, args, registry, sandbox=None):
    return tools[name].ainvoke(
        args, job_registry=registry, sandbox=sandbox, sandbox_policy=sandbox
    )


@pytest.mark.asyncio
async def test_background_shell_lifecycle_and_read(temp_workspace, monkeypatch):
    async def run(*args, **kwargs):
        return "background-output"

    monkeypatch.setattr("XBotv2.coretools.shell.run_shell_command", run)
    registry, tools = make_tools(temp_workspace)
    assert set(tools) == {
        "shell", "start_shell", "list_shells",
        "wait_shell", "read_shell", "cancel_shell",
    }
    assert "background" not in tools["shell"].parameters["properties"]

    started = await invoke(
        tools, "start_shell", {"command": "printf background-output"}, registry
    )
    job_id = started.data["id"]
    waited = await invoke(tools, "wait_shell", {"ids": [job_id]}, registry)
    assert waited.data["ready"][0]["status"] == "completed"
    assert waited.data["pending"] == []
    assert waited.data["timed_out"] is False
    read = await invoke(tools, "read_shell", {"id": job_id}, registry)
    assert read.data["content"] == "background-output"
    assert read.data["eof"] is True
    listed = await invoke(tools, "list_shells", {}, registry)
    assert [item["id"] for item in listed.data["shells"]] == [job_id]


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

    monkeypatch.setattr("XBotv2.coretools.shell.run_shell_command", run)
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
        tools, "start_shell",
        {"command": "install dependency",
         "sandbox_permissions": "require_escalated",
         "justification": "Install a required dependency."},
        registry,
        sandbox=object(),
    )
    await invoke(tools, "wait_shell", {"ids": [background.data["id"]]}, registry)

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

    monkeypatch.setattr("XBotv2.coretools.shell.run_shell_command", run)
    registry, tools = make_tools(temp_workspace)
    started = await invoke(
        tools, "start_shell", {"command": "generate output"}, registry
    )
    job = registry.get(started.data["id"])
    await job.runner_task
    await registry.wait([job.id])

    assert len(registry.snapshot(job)["output"]) < 2_100
    read = await invoke(
        tools, "read_shell", {"id": job.id, "max_bytes": 20_000}, registry
    )
    assert read.data["content"] == full_output


@pytest.mark.asyncio
async def test_cancel_shell_stops_process(temp_workspace, monkeypatch):
    async def run(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("XBotv2.coretools.shell.run_shell_command", run)
    registry, tools = make_tools(temp_workspace)
    started = await invoke(tools, "start_shell", {"command": "sleep 30"}, registry)
    job = registry.get(started.data["id"])
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

    monkeypatch.setattr("XBotv2.coretools.shell.run_shell_command", run)
    completions = []
    registry, tools = make_tools(temp_workspace)

    async def record_completion(task):
        completions.append(task)

    registry.on_complete = record_completion
    started = await invoke(tools, "start_shell", {"command": "sleep 30"}, registry)
    await asyncio.sleep(0)

    await asyncio.wait_for(registry.shutdown(), timeout=1)

    assert registry.get_or_none(started.data["id"]) is None
    assert completions == []


@pytest.mark.asyncio
async def test_wait_shell_returns_exit_code_for_completed_job(
    temp_workspace, monkeypatch
):
    async def run(*args, **kwargs):
        return "ok"

    monkeypatch.setattr("XBotv2.coretools.shell.run_shell_command", run)
    registry, tools = make_tools(temp_workspace)
    started = await invoke(tools, "start_shell", {"command": "true"}, registry)
    waited = await invoke(tools, "wait_shell", {"ids": [started.data["id"]]}, registry)
    assert waited.data["ready"][0]["exit_code"] == 0


@pytest.mark.asyncio
async def test_escalated_background_shell_requires_approval(
    temp_workspace, monkeypatch
):
    async def run(*args, **kwargs):
        return "ran"

    monkeypatch.setattr("XBotv2.coretools.shell.run_shell_command", run)
    sandbox = SandboxPolicy(
        {"enabled": True, "external_write": "ask"},
        workspace_root=str(temp_workspace),
    )
    registry = ToolRegistry()
    registry.register(
        next(tool for tool in SHELL_TOOLS if tool.name == "start_shell"),
        sandbox_mode="sandboxed",
    )
    job_registry = JobRegistry()
    events = []

    async def approve(event, **kwargs):
        events.append(event)
        return {"status": "answered", "decision": "allow", "scope": "once"}

    results = await execute_tools(
        [ToolCall("c1", "start_shell", {
            "command": "pwd",
            "sandbox_permissions": "require_escalated",
            "justification": "Need host access.",
        })],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="allow"),
        permission_interaction_handler=approve,
        job_registry=job_registry,
    )

    assert results[0].status == "success"
    assert events and events[0]["data"]["source"] == "sandbox"
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
    registry.register(
        next(tool for tool in SHELL_TOOLS if tool.name == "start_shell"),
        sandbox_mode="sandboxed",
    )
    job_registry = JobRegistry()

    async def deny(event, **kwargs):
        del event, kwargs
        return {"status": "answered", "decision": "deny"}

    results = await execute_tools(
        [ToolCall("c1", "start_shell", {
            "command": "pwd",
            "sandbox_permissions": "require_escalated",
            "justification": "Need host access.",
        })],
        registry,
        sandbox_policy=sandbox,
        permission_system=PermissionSystem(default_decision="allow"),
        permission_interaction_handler=deny,
        job_registry=job_registry,
    )

    assert results[0].status == "error"
    assert "denied" in results[0].content
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
        "XBotv2.coretools.shell.subprocess.Popen", create_process
    )
    monkeypatch.setattr(
        "XBotv2.coretools.shell._signal_process", signal_process
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
        "XBotv2.coretools.shell.subprocess.Popen", create_process
    )

    result = await run_shell_command("generate", cwd=str(temp_workspace))

    assert result == "x" * 100_001
