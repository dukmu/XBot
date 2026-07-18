import asyncio

import pytest

from xbotv2.core.background_tasks import BackgroundTaskManager
from xbotv2.core.builtin_tools.shell import run_shell_command
from xbotv2.tools.sandbox import SandboxPolicy


@pytest.mark.asyncio
async def test_background_task_lifecycle_and_full_result(temp_workspace, monkeypatch):
    async def run(*args, **kwargs):
        return "background-output"

    monkeypatch.setattr("xbotv2.core.background_tasks.run_shell_command", run)
    updates = []
    completions = []
    manager = BackgroundTaskManager(workspace_root=str(temp_workspace))

    async def record_update(task):
        updates.append(dict(task))

    async def record_completion(task):
        completions.append(dict(task))

    manager.on_update = record_update
    manager.on_complete = record_completion

    tools = {tool.name: tool for tool in manager.tools}
    assert set(tools) == {"shell", "list_tasks", "stop_task"}
    background = tools["shell"].parameters["properties"]["background"]
    assert background == {"type": "boolean"}

    started = await manager.shell(
        "printf background-output", background=True
    )
    task_id = started.data["task_id"]
    await manager._tasks[task_id].runner

    result = await manager.list_tasks(task_id)
    assert [item["status"] for item in updates] == [
        "pending", "running", "completed",
    ]
    assert completions[0]["status"] == "completed"
    assert result.data["output"] == "background-output"


@pytest.mark.asyncio
async def test_foreground_shell_defaults_to_workspace_when_sandbox_is_disabled(
    temp_workspace,
):
    sandbox = SandboxPolicy(enabled=False, workspace_root=temp_workspace)
    manager = BackgroundTaskManager(
        workspace_root=str(temp_workspace),
        sandbox=sandbox,
    )

    result = await manager.shell("pwd")

    assert result.status == "success"
    assert result.content.strip() == str(temp_workspace)


@pytest.mark.asyncio
async def test_task_events_bound_output_but_result_keeps_cacheable_content(
    temp_workspace, monkeypatch
):
    full_output = "x" * 13_000

    async def run(*args, **kwargs):
        return full_output

    monkeypatch.setattr("xbotv2.core.background_tasks.run_shell_command", run)
    manager = BackgroundTaskManager(workspace_root=str(temp_workspace))
    updates = []

    async def record_update(task):
        updates.append(task)

    manager.on_update = record_update
    started = await manager.start_task("generate output")
    await manager._tasks[started.data["task_id"]].runner

    result = await manager.list_tasks(started.data["task_id"])

    assert len(updates[-1]["output"]) < 2_100
    assert len(result.data["output"]) < 2_100
    assert full_output in result.content


@pytest.mark.asyncio
async def test_stop_task_cancels_process_and_reports_stopped(
    temp_workspace, monkeypatch
):
    async def run(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("xbotv2.core.background_tasks.run_shell_command", run)
    running = asyncio.Event()
    manager = BackgroundTaskManager(workspace_root=str(temp_workspace))

    async def record_update(task):
        if task["status"] == "running":
            running.set()

    manager.on_update = record_update
    started = await manager.start_task("sleep 30")
    await asyncio.wait_for(running.wait(), timeout=1)

    result = await asyncio.wait_for(
        manager.stop_task(started.data["task_id"]), timeout=1
    )

    assert result.status == "success"
    assert result.data["status"] == "stopped"
    assert manager._tasks[started.data["task_id"]].runner.done()


@pytest.mark.asyncio
async def test_close_stops_tasks_without_completion_delivery(
    temp_workspace, monkeypatch
):
    async def run(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("xbotv2.core.background_tasks.run_shell_command", run)
    completions = []
    manager = BackgroundTaskManager(workspace_root=str(temp_workspace))

    async def record_completion(task):
        completions.append(task)

    manager.on_complete = record_completion
    started = await manager.start_task("sleep 30")
    await asyncio.sleep(0)

    await asyncio.wait_for(manager.close(), timeout=1)

    assert started.data["task_id"] in manager._tasks
    assert manager._tasks[started.data["task_id"]].status == "stopped"
    assert completions == []


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
        "xbotv2.core.builtin_tools.shell.subprocess.Popen", create_process
    )
    monkeypatch.setattr(
        "xbotv2.core.builtin_tools.shell._signal_process", signal_process
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
async def test_host_shell_returns_complete_output_for_common_cache(temp_workspace, monkeypatch):
    class Process:
        pid = 123
        returncode = 0

        def poll(self):
            return self.returncode

    def create_process(*args, stdout, **kwargs):
        stdout.write(b"x" * 100_001)
        return Process()

    monkeypatch.setattr(
        "xbotv2.core.builtin_tools.shell.subprocess.Popen", create_process
    )

    result = await run_shell_command("generate", cwd=str(temp_workspace))

    assert result == "x" * 100_001
