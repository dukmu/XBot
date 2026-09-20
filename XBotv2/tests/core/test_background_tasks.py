import asyncio
import json

import pytest

from XBotv2.application import RUNTIME_EVENT
from XBotv2.jobs import JobKind, JobResult, JobStatus
from XBotv2.jobs.plugin import JobsRuntimeComponent
from XBotv2.jobs.contracts import JobsConfig
from XBotv2.jobs.registry import JobRegistry
from XBotv2.core.tools import ToolCall
from XBotv2.coretools import shell as shell_module
from XBotv2.coretools.shell import run_shell_command, shell_tools
from XBotv2.permissions.system import PermissionSystem
from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.agentloop import Events
from XBotv2.commands.plugin import CommandsService
from XBotv2.permissions.approval import ApprovalService
from XBotv2.interactions.interactions import InteractionWaiter
from XBotv2.application.client_events import ClientEventRouter
from XBotv2.sandbox.contracts import SandboxConfig
from XBotv2.tests.helpers import make_tool_ctx
import xcore
from XBotv2.sandbox.policy import SandboxPolicy


class _RecordingPublisher:
    """A TaskEventPort stub recording completed task ids."""

    def __init__(self, completions: list[str]) -> None:
        self._completions = completions

    async def emit(self, event, *args) -> None:
        from XBotv2.jobs.contracts import JOB_COMPLETED

        if event == JOB_COMPLETED and args:
            snapshot = args[0]
            self._completions.append(snapshot.job_id)


def make_tools(temp_workspace, *, sandbox=None, approval_layer=None, publisher=None):
    registry = JobRegistry(publisher=publisher)
    tools = {
        tool.name: tool
        for tool in shell_tools(
            sandbox, registry, str(temp_workspace),
            approval_layer=approval_layer,
        )
    }
    return registry, tools


def invoke(tools, name, args, registry, sandbox=None):
    del registry, sandbox
    return tools[name].ainvoke(args)


def patch_shell_executor(monkeypatch, replacement):
    """Patch the executor resolved by session-bound shell Tools."""
    monkeypatch.setattr(shell_module, "run_shell_command", replacement)


@pytest.mark.asyncio
async def test_jobs_plugin_owns_updates_and_completion_delivery():
    class Engine:
        def __init__(self):
            self.injected = []

        async def inject(self, content, **kwargs):
            self.injected.append((content, kwargs))

    ctx = xcore.Context()
    engine = Engine()
    ctx.set("commands", CommandsService(ownership="caller"))
    ctx.set("engine", engine)
    runtime_events = []

    async def record(event):
        runtime_events.append(event.client_event)

    ctx.on(RUNTIME_EVENT, record)
    JobsRuntimeComponent().apply(ctx, JobsConfig())
    from XBotv2.jobs import JobSnapshot

    snapshot = JobSnapshot(
        job_id="sh_1",
        kind="shell",
        command="printf x",
        cwd="/workspace",
        status="completed",
        created_at=1.0,
        started_at=2.0,
        finished_at=3.0,
        output="x",
    )

    assert ctx.jobs is not None
    from XBotv2.jobs.contracts import JOB_COMPLETED, JOB_UPDATED

    await ctx.emit(JOB_UPDATED, snapshot)
    await ctx.emit(JOB_COMPLETED, snapshot)

    assert [event.type for event in runtime_events] == [
        "job_updated",
        "completion_notice",
    ]
    assert runtime_events[1].data["kind"] == "background_job"
    assert len(engine.injected) == 1
    assert engine.injected[0][1]["source"] == "sh_1"
    assert "background_job" in engine.injected[0][0]


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
    # An active approval layer is required for escalation: without one the
    # tool fails closed before execution.
    registry, tools = make_tools(
        temp_workspace,
        sandbox=object(),
        approval_layer=object(),
    )
    no_layer_registry, no_layer_tools = make_tools(
        temp_workspace,
        sandbox=object(),
    )

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

    # Without an approval layer the same call is refused (fail closed).
    refused = await invoke(
        no_layer_tools, "shell",
        {"command": "install dependency",
         "sandbox_permissions": "require_escalated",
         "justification": "Install a required dependency."},
        no_layer_registry,
        sandbox=object(),
    )
    assert refused.status == "error"
    assert refused.content == (
        "Sandbox escape requires an active approval layer; none is mounted "
        "(enable the permissions plugin)"
    )


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
    await registry.wait([job.id])

    assert len(registry.snapshot(job).output) < 2_100
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
    assert (await registry.wait([job.id])).pending == []


@pytest.mark.asyncio
async def test_shutdown_stops_jobs_without_completion_delivery(
    temp_workspace, monkeypatch
):
    async def run(*args, **kwargs):
        await asyncio.Event().wait()

    patch_shell_executor(monkeypatch, run)
    completions = []
    publisher = _RecordingPublisher(completions)
    registry, tools = make_tools(temp_workspace, publisher=publisher)

    started = await invoke(tools, "shell", {"command": "sleep 30", "background": True}, registry)
    await asyncio.sleep(0)

    await asyncio.wait_for(registry.shutdown(), timeout=1)

    assert registry.get_or_none(started.content.split("Started ")[1]) is None
    assert completions == []


class _ControllableRunner:
    """A JobRunner that blocks until released; records invocations."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.run_count = 0

    async def run(self, job, ctx):
        del job, ctx
        self.run_count += 1
        self.started.set()
        await self.release.wait()
        return JobResult(summary="ran")

    async def cancel(self, job) -> None:
        del job


async def _start_blocking_and_queued(registry):
    """Start one blocking job and queue a second behind its kind semaphore."""
    first = _ControllableRunner()
    job1 = await registry.create(kind=JobKind.SHELL)
    registry.start(job1.id, first)
    await asyncio.wait_for(first.started.wait(), timeout=1)
    queued = _ControllableRunner()
    job2 = await registry.create(kind=JobKind.SHELL)
    registry.start(job2.id, queued)
    await asyncio.sleep(0)  # let the second task block on the semaphore
    return first, job1, queued, job2


@pytest.mark.asyncio
async def test_cancel_queued_job_never_starts_and_completes_once():
    """Cancelling a job queued behind its kind's semaphore must cancel the
    underlying task: the runner must never run and the terminal transition
    (and its completion notice) must be published exactly once."""
    completions: list[str] = []
    registry = JobRegistry(
        limits={JobKind.SHELL: 1},
        publisher=_RecordingPublisher(completions),
    )

    first, job1, queued, job2 = await _start_blocking_and_queued(registry)
    assert job1.status is JobStatus.RUNNING
    assert job2.status is JobStatus.PENDING
    assert queued.run_count == 0

    result = await registry.cancel(job2.id)
    assert result.cancelled is True
    assert job2.status is JobStatus.CANCELLED
    assert queued.run_count == 0

    # Free the slot: the cancelled task owns no future, so nothing resumes.
    first.release.set()
    await registry.wait([job1.id])
    for _ in range(5):
        await asyncio.sleep(0)

    assert queued.run_count == 0
    assert job2.status is JobStatus.CANCELLED
    # Exactly one completion notice per job — the cancelled job never
    # reported a second, resumed completion.
    assert sorted(completions) == [job1.id, job2.id]


@pytest.mark.asyncio
async def test_shutdown_cancels_queued_job_before_it_starts():
    """shutdown() must cancel queued tasks too, so a job queued behind the
    semaphore cannot spawn and run against a closing (or closed) session."""
    registry = JobRegistry(limits={JobKind.SHELL: 1})
    first, job1, queued, job2 = await _start_blocking_and_queued(registry)
    del job1
    assert job2.status is JobStatus.PENDING

    snapshots = await asyncio.wait_for(registry.shutdown(), timeout=1)
    await asyncio.sleep(0)

    assert queued.run_count == 0
    assert all(snap.status == "stopped" for snap in snapshots)
    del first


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
        SandboxConfig(enabled=True, external_write="deny"),
        workspace_root=str(temp_workspace),
    )
    registry = ToolRegistry()
    job_registry = JobRegistry()
    registry.register(next(
        tool
        for tool in shell_tools(
            sandbox, job_registry, str(temp_workspace), approval_layer=object()
        )
        if tool.name == "shell"
    ))
    events = []

    async def approve(event, **_kwargs):
        events.append(event.model_dump(mode="json"))
        return {"status": "answered", "decision": "allow", "scope": "once"}

    service_ctx = xcore.Context()
    client_events = ClientEventRouter()
    client_events.install(approve)
    approval = ApprovalService(service_ctx, client_events, InteractionWaiter())
    ctx = make_tool_ctx(
        registry,
        sandbox=sandbox,
        permissions=PermissionSystem(default_decision="allow"),
        approval=approval,
        base=service_ctx,
    )
    results = await ctx.tools.execute_all(
        [ToolCall(id="c1", name="shell", args={
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
    await job_registry.wait([job.id])
    assert job.status.value == "completed"


@pytest.mark.asyncio
async def test_denied_background_shell_escalation_creates_no_job(
    temp_workspace,
):
    sandbox = SandboxPolicy(
        SandboxConfig(enabled=True, external_write="deny"),
        workspace_root=str(temp_workspace),
    )
    registry = ToolRegistry()
    job_registry = JobRegistry()
    registry.register(next(
        tool
        for tool in shell_tools(sandbox, job_registry, str(temp_workspace))
        if tool.name == "shell"
    ))

    async def deny(event, **_kwargs):
        del event
        return {"status": "answered", "decision": "deny", "scope": "once"}

    service_ctx = xcore.Context()
    client_events = ClientEventRouter()
    client_events.install(deny)
    approval = ApprovalService(service_ctx, client_events, InteractionWaiter())
    ctx = make_tool_ctx(
        registry,
        sandbox=sandbox,
        permissions=PermissionSystem(default_decision="allow"),
        approval=approval,
        base=service_ctx,
    )
    results = await ctx.tools.execute_all(
        [ToolCall(id="c1", name="shell", args={
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


@pytest.mark.asyncio
async def test_pipeline_denies_escalation_without_approval_guard(temp_workspace):
    """The execution pipeline fails closed: an escaping tool call without an
    approval-capable guard in the pipeline never reaches dispatch, even when
    the tool itself is mounted with an approval layer."""
    job_registry = JobRegistry()
    sandbox = SandboxPolicy(enabled=False, workspace_root=str(temp_workspace))
    shell = next(
        tool
        for tool in shell_tools(
            sandbox, job_registry, str(temp_workspace), approval_layer=object()
        )
        if tool.name == "shell"
    )
    registry = ToolRegistry()
    registry.register(shell)
    ctx = make_tool_ctx(registry, sandbox=sandbox)  # no permissions guard

    results = await ctx.tools.execute_all([
        ToolCall(id="c1", name="shell", args={
            "command": "pwd",
            "sandbox_permissions": "require_escalated",
            "justification": "Need host access.",
        }),
    ])

    assert results[0].status == "error"
    assert "approval layer" in results[0].content
    assert job_registry.all() == []
