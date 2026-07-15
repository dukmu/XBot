"""Session-owned background shell tasks."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from xbotv2.api.tools import Tool, ToolResult
from xbotv2.core.builtin_tools.shell import execute_shell, run_shell_command


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
    """Own background processes for one live session."""

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
            Tool.from_function(self.stop_task, name="stop_task"),
        )

    async def shell(
        self,
        command: str,
        cwd: str | None = None,
        background: bool = False,
    ) -> ToolResult:
        """Run a shell command, optionally as a session-owned background task."""
        if background:
            return await self.start_task(command, cwd)
        return await execute_shell(command, cwd, sandbox=self.sandbox)

    async def start_task(self, command: str, cwd: str | None = None) -> ToolResult:
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
            self._run(task), name=f"xbotv2-{task_id}"
        )
        return ToolResult.success(
            f"Started {task_id}: {command}", data=task.snapshot()
        )

    async def list_tasks(self, task_id: str | None = None) -> ToolResult:
        """List background tasks, or return the full result for one task."""
        if task_id:
            task = self._tasks.get(task_id)
            if task is None:
                return ToolResult.failure("task_not_found", f"Unknown task: {task_id}")
            content: Any = task.snapshot(full_output=True)
            data: Any = task.snapshot()
        else:
            data = [task.snapshot() for task in self._tasks.values()]
            content = data
        return ToolResult.success(
            json.dumps(content, ensure_ascii=False), data=data
        )

    async def stop_task(self, task_id: str) -> ToolResult:
        """Stop a running background task."""
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
            task for task in self._tasks.values()
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

    async def _run(self, task: BackgroundTask) -> None:
        task.status = "running"
        task.started_at = time.time()
        await self._notify(task)
        try:
            task.output = await run_shell_command(
                task.command,
                cwd=task.cwd,
                sandbox=self.sandbox,
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
