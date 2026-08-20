"""Human commands owned by the jobs component (``/tasks``, ``/task``).

The handlers operate on the injected ``ctx.services.jobs`` registry
directly; the application layer does not implement job-domain logic.
"""

from __future__ import annotations

from typing import Any

from XBotv2.core.commands import (
    Command,
    CommandResult,
    command_usage,
    guard_command,
    split_command_args,
)


async def tasks_command(ctx: Any, raw_args: str) -> CommandResult:
    parts = split_command_args(raw_args)
    if parts not in ([], ["ps"]):
        return command_usage("/tasks [ps]")
    registry = ctx.services.get("jobs")
    tasks = (
        [registry.snapshot(job) for job in registry.all()]
        if registry is not None
        else []
    )
    message = "No background tasks." if not tasks else "\n".join(
        f"{task['kind']}  {task['task_id']}  {task['status']}  {task['command']}"
        for task in tasks
    )
    return CommandResult(message)


async def task_command(ctx: Any, raw_args: str) -> CommandResult:
    parts = split_command_args(raw_args)
    if len(parts) == 2 and parts[0] == "stop":
        registry = ctx.services.get("jobs")
        if registry is None:
            return _error("Jobs registry is not loaded.")
        job = registry.get_or_none(parts[1])
        if job is None:
            return _error(f"Unknown task: {parts[1]}")
        await registry.cancel(parts[1])
        return CommandResult(
            f"Stopped background task {parts[1]}."
        )
    if parts == ["stopall"]:
        registry = ctx.services.get("jobs")
        tasks = await registry.stop_all() if registry is not None else []
        return CommandResult(
            f"Stopped {len(tasks)} background task(s)."
        )
    return command_usage("/task stop <id> | /task stopall")


def _error(message: str) -> CommandResult:
    return CommandResult(message, status="error")


JOBS_COMMANDS: tuple[Command, ...] = (
    Command(
        name="tasks",
        description="List background tasks",
        handler=guard_command(tasks_command),
        usage="/tasks [ps]",
    ),
    Command(
        name="task",
        description="Stop background tasks",
        handler=guard_command(task_command),
        usage="/task stop <id> | /task stopall",
    ),
)


__all__ = ["JOBS_COMMANDS", "tasks_command", "task_command"]
