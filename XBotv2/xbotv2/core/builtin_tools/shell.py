"""Shell execution and session-owned background shell tasks."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from xbotv2.api.tools import Tool, ToolResult


TaskCallback = Callable[[dict[str, Any]], Awaitable[None]]
_TERMINAL_STATES = {"completed", "failed", "stopped"}


@dataclass(slots=True)
class BackgroundTask:
    id: str
    command: str
    cwd: str
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    output: str = ""
    error: str = ""
    runner: asyncio.Task[None] | None = field(default=None, repr=False)

    def snapshot(self, *, full_output: bool = False) -> dict[str, Any]:
        command = self.command if full_output else _preview(self.command, 1000)
        output = self.output if full_output else _preview(self.output, 2000)
        error = self.error if full_output else _preview(self.error, 2000)
        return {
            "task_id": self.id,
            "kind": "shell",
            "command": command,
            "cwd": self.cwd,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "output": output,
            "error": error,
        }


class BackgroundTaskManager:
    """Own shell execution and background processes for one live session."""

    def __init__(self, *, workspace_root: str, sandbox: Any = None) -> None:
        self.workspace_root = workspace_root
        self.sandbox = sandbox
        self.on_update: TaskCallback | None = None
        self.on_complete: TaskCallback | None = None
        self._tasks: dict[str, BackgroundTask] = {}
        self._next_id = 1
        self._closing = False

    @property
    def tools(self) -> tuple[Tool, ...]:
        return (
            Tool.from_function(self.shell, name="shell"),
            Tool.from_function(self.list_tasks, name="list_tasks"),
            Tool.from_function(self.wait_task, name="wait_task"),
            Tool.from_function(self.stop_task, name="stop_task"),
        )

    async def shell(
        self,
        command: str,
        cwd: str | None = None,
        background: bool = False,
        sandbox_permissions: Literal[
            "use_default", "require_escalated"
        ] = "use_default",
        justification: str | None = None,
        *,
        sandbox: Any = None,
    ) -> ToolResult:
        """Run a shell command in the foreground or as a background task.

        Foreground mode returns the completed command's output. Background mode
        returns a task ID immediately; use ``wait_task`` when later work depends
        on completion, or ``list_tasks`` to inspect it without waiting. Starting
        a background task does not mean its command succeeded. Commands must be
        non-interactive.

        Tool status follows the final exit code. If a nonzero exit is an
        expected result, the command must verify that condition and then exit
        zero. Unexpected failures must remain nonzero.

        Commands run in the configured sandbox by default. To run the complete
        command outside it, request ``require_escalated`` and provide a concrete
        justification; XBot asks the human for approval before execution.

        Args:
            command: Complete shell command to execute.
            cwd: Working directory. Defaults to the session workspace root.
            background: Start a session-owned task and return immediately when true.
            sandbox_permissions: ``use_default`` runs inside the configured
                sandbox; ``require_escalated`` requests execution outside it.
            justification: Required explanation when requesting escalation.
        """
        if sandbox_permissions == "require_escalated":
            if not justification or not justification.strip():
                return ToolResult.failure(
                    "invalid_sandbox_request",
                    "justification is required for an escalated shell command",
                )
        if background:
            return await self.start_task(
                command,
                cwd,
                escalated=(sandbox_permissions == "require_escalated"),
            )
        active_sandbox = (
            None
            if sandbox_permissions == "require_escalated"
            else sandbox or self.sandbox
        )
        try:
            output = await run_shell_command(
                command,
                cwd=cwd or self.workspace_root,
                sandbox=active_sandbox,
                timeout_seconds=0,
            )
        except Exception as exc:
            return ToolResult.failure("command_failed", str(exc))
        return ToolResult.success(output)

    async def start_task(
        self,
        command: str,
        cwd: str | None = None,
        *,
        escalated: bool = False,
    ) -> ToolResult:
        """Start a shell command in the background and return its task ID."""
        if not command.strip():
            return ToolResult.failure("invalid_command", "Command cannot be empty")
        if self._closing:
            return ToolResult.failure("session_closing", "Session is closing")
        task_id = f"task-{self._next_id}"
        self._next_id += 1
        task = BackgroundTask(task_id, command, cwd or self.workspace_root)
        self._tasks[task_id] = task
        await self._notify(task)
        if self._closing or task.status in _TERMINAL_STATES:
            return ToolResult.failure("session_closing", "Session is closing")
        task.runner = asyncio.create_task(
            self._run(task, escalated), name=f"xbotv2-{task_id}"
        )
        return ToolResult.success(
            f"Started {task_id}; completion is pending: {command}",
            data=task.snapshot(),
        )

    async def list_tasks(self, task_id: str | None = None) -> ToolResult:
        """Inspect session-owned background shell tasks.

        Omit the ID only to discover task IDs and statuses. With an ID, return
        that task's authoritative status and complete captured output. This
        call never waits for a running task. Tasks are runtime state and do not
        survive session shutdown.

        Args:
            task_id: Optional ID returned by shell(background=true). Omit to list
                all tasks; provide it to retrieve one task's current result.
        """
        if task_id:
            task = self._tasks.get(task_id)
            if task is None:
                return ToolResult.failure("task_not_found", f"Unknown task: {task_id}")
            content: Any = task.snapshot(full_output=True)
            data: Any = task.snapshot()
        else:
            data = [task.snapshot() for task in self._tasks.values()]
            content = data
        return ToolResult.success(json.dumps(content, ensure_ascii=False), data=data)

    async def wait_task(self, task_id: str) -> ToolResult:
        """Wait for one background shell task and return its complete result.

        Use this when subsequent work depends on a task reaching completed,
        failed, or stopped status. Cancelling this Tool call stops only the
        wait; the background task continues until it finishes or ``stop_task``
        is called. Inspect the returned terminal status and output; waiting for
        a task is not evidence that its command succeeded.

        Args:
            task_id: Exact task ID returned by shell(background=true).
        """
        task = self._tasks.get(task_id)
        if task is None:
            return ToolResult.failure("task_not_found", f"Unknown task: {task_id}")
        if task.runner is not None and not task.runner.done():
            await asyncio.shield(task.runner)
        return await self.list_tasks(task_id)

    async def stop_task(self, task_id: str) -> ToolResult:
        """Stop one session-owned background shell task.

        This is idempotent for a task that has already reached a terminal state.
        Use ``list_tasks`` first when the task ID or status is unknown.

        Args:
            task_id: Exact task ID returned by shell(background=true).
        """
        task = self._tasks.get(task_id)
        if task is None:
            return ToolResult.failure("task_not_found", f"Unknown task: {task_id}")
        if task.status in _TERMINAL_STATES:
            return ToolResult.success(
                f"{task_id} is already {task.status}", data=task.snapshot()
            )
        await self._cancel(task)
        return ToolResult.success(f"Stopped {task_id}", data=task.snapshot())

    async def stop_all(self) -> list[dict[str, Any]]:
        active = [
            task
            for task in self._tasks.values()
            if task.status not in _TERMINAL_STATES
        ]
        await asyncio.gather(*(self._cancel(task) for task in active))
        return [task.snapshot() for task in active]

    def snapshots(self) -> list[dict[str, Any]]:
        return [task.snapshot() for task in self._tasks.values()]

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        await self.stop_all()
        self.on_update = None
        self.on_complete = None

    async def _run(self, task: BackgroundTask, escalated: bool) -> None:
        task.status = "running"
        task.started_at = time.time()
        await self._notify(task)
        try:
            task.output = await run_shell_command(
                task.command,
                cwd=task.cwd,
                sandbox=None if escalated else self.sandbox,
                timeout_seconds=0,
            )
            task.status = "completed"
        except asyncio.CancelledError:
            task.status = "stopped"
        except Exception as exc:  # noqa: BLE001 - task failures are state
            task.status = "failed"
            task.error = str(exc)
        finally:
            task.finished_at = time.time()
            await self._notify(task)
            if not self._closing and self.on_complete is not None:
                await self.on_complete(task.snapshot())

    async def _cancel(self, task: BackgroundTask) -> None:
        runner = task.runner
        if runner is None:
            task.status = "stopped"
            task.finished_at = time.time()
            await self._notify(task)
            return
        if not runner.done():
            runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    async def _notify(self, task: BackgroundTask) -> None:
        if self.on_update is not None:
            await self.on_update(task.snapshot())


def _preview(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n[truncated; {len(value) - limit} characters omitted]"


async def run_shell_command(
    command: str,
    *,
    cwd: str | None = None,
    sandbox=None,
    timeout_seconds: float | None = 0,
) -> str:
    """Run a shell command with cancellation-safe process cleanup."""
    shell = _default_shell()
    if sandbox is not None and sandbox.enabled:
        return await sandbox.run_shell(
            command,
            shell=shell,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )

    with tempfile.TemporaryFile() as output_file:
        proc = subprocess.Popen(
            [shell, "/c" if os.name == "nt" else "-lc", command],
            cwd=cwd,
            stdout=output_file,
            stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
        try:
            await _wait_process(proc, timeout_seconds)
        except BaseException:
            if proc.poll() is None:
                _signal_process(proc)
            await _wait_process(proc, None)
            raise
        output_file.seek(0)
        output = output_file.read().decode("utf-8", errors="replace")
    output = output or "(no output)"
    if proc.returncode:
        raise RuntimeError(
            f"Command failed with exit code {proc.returncode}: {output.strip()}"
        )
    return output


def _default_shell() -> str:
    variable = "COMSPEC" if os.name == "nt" else "SHELL"
    shell = os.environ.get(variable)
    if not shell:
        raise RuntimeError(f"{variable} is not set in the XBot process environment")
    return shell


def _signal_process(proc: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass


async def _wait_process(
    proc: subprocess.Popen[bytes], timeout_seconds: float | None
) -> None:
    loop = asyncio.get_running_loop()
    deadline = (
        loop.time() + timeout_seconds
        if timeout_seconds is not None and timeout_seconds > 0
        else None
    )
    while proc.poll() is None:
        if deadline is not None and loop.time() >= deadline:
            raise asyncio.TimeoutError
        await asyncio.sleep(0.05)
