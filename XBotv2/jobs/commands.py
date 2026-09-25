"""Human job command declarations and service binding factory."""

from __future__ import annotations

from XBotv2.jobs.contracts import JobsCommandPort

from XBotv2.commands import (
    Command,
    CommandResult,
    command_usage,
    guard_command,
    split_command_args,
)


def build_jobs_commands(jobs: JobsCommandPort) -> tuple[Command, ...]:
    async def jobs_command(raw_args: str) -> CommandResult:
        parts = split_command_args(raw_args)
        if parts in ([], ["ps"]):
            views = jobs.views()
            message = "No background jobs." if not views else "\n".join(
                f"{job.kind}  {job.id}  {job.state}  {job.label}"
                for job in views
            )
            return CommandResult(message)
        if len(parts) == 2 and parts[0] == "stop":
            if jobs.get_or_none(parts[1]) is None:
                return CommandResult(f"Unknown job: {parts[1]}", status="error")
            await jobs.cancel(parts[1])
            return CommandResult(
                f"Stopped background job {parts[1]}.",
                effects=("jobs",),
            )
        if parts == ["stopall"]:
            stopped = await jobs.stop_all()
            return CommandResult(
                f"Stopped {len(stopped)} background job(s).",
                effects=("jobs",),
            )
        return command_usage("/jobs [ps] | /jobs stop <id> | /jobs stopall")

    return (
        Command(
            name="jobs",
            description="List or stop background jobs",
            handler=guard_command(jobs_command),
            effects=("jobs",),
            usage="/jobs [ps] | /jobs stop <id> | /jobs stopall",
            examples=("/jobs", "/jobs stop job-3", "/jobs stopall"),
        ),
    )


__all__ = ["JobsCommandPort", "build_jobs_commands"]
