"""Human job command declarations and service binding factory."""

from __future__ import annotations

from typing import Protocol

from XBotv2.core.commands import (
    Command,
    CommandResult,
    command_usage,
    guard_command,
    split_command_args,
)


class JobsCommandPort(Protocol):
    def snapshots(self) -> list[dict[str, object]]: ...

    def get_or_none(self, job_id: str) -> object | None: ...

    async def cancel(self, job_id: str) -> object: ...

    async def stop_all(self) -> list[dict[str, object]]: ...


def build_jobs_commands(jobs: JobsCommandPort) -> tuple[Command, ...]:
    async def tasks_command(raw_args: str) -> CommandResult:
        parts = split_command_args(raw_args)
        if parts not in ([], ["ps"]):
            return command_usage("/tasks [ps]")
        tasks = jobs.snapshots()
        message = "No background tasks." if not tasks else "\n".join(
            f"{task['kind']}  {task['task_id']}  {task['status']}  {task['command']}"
            for task in tasks
        )
        return CommandResult(message)

    async def task_command(raw_args: str) -> CommandResult:
        parts = split_command_args(raw_args)
        if len(parts) == 2 and parts[0] == "stop":
            if jobs.get_or_none(parts[1]) is None:
                return CommandResult(f"Unknown task: {parts[1]}", status="error")
            await jobs.cancel(parts[1])
            return CommandResult(f"Stopped background task {parts[1]}.")
        if parts == ["stopall"]:
            tasks = await jobs.stop_all()
            return CommandResult(f"Stopped {len(tasks)} background task(s).")
        return command_usage("/task stop <id> | /task stopall")

    return (
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


__all__ = ["JobsCommandPort", "build_jobs_commands"]
