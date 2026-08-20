"""Jobs component: the background job registry as an XCore service.

The registry is created by this plugin (mounted after the session component,
whose ``ctx.runtime`` supplies the concurrency limits) and provided as
``ctx.jobs``.  Job lifecycle — waiting, cancellation, output storage — lives
in the registry; domain adapters (subagents, shell) implement ``JobRunner``.
"""

from __future__ import annotations

from typing import Any

from XBotv2.core.errors import OperationError
from XBotv2.core.events import EventContext, Events
from XBotv2.core.jobs import JobKind
from XBotv2.jobs.commands import build_jobs_commands
from XBotv2.jobs.protocol import TaskUpdatedData
from XBotv2.jobs.registry import JobRegistry
from XBotv2.core.operations import EmptyRequest
from XBotv2.jobs.contracts import (
    LIST_TASKS,
    STOP_ALL_TASKS,
    STOP_TASK,
    StopTask,
    StoppedTasks,
    TaskCatalog,
    task_snapshot,
)
from XBotv2.session.contracts import PREPARE_FORK, PrepareFork


class JobsComponent:
    inject = ['session', 'commands']
    """Register the job registry as ``ctx.jobs``."""

    name = "xbot.jobs"

    def apply(self, ctx: Any, config: Any = None) -> None:
        max_concurrent = int((config or {}).get("max_concurrent_subagents", 4))
        registry = JobRegistry(limits={JobKind.SUBAGENT: max_concurrent})
        ctx.set("jobs", registry)
        for command in build_jobs_commands(registry):
            ctx.commands.register(command)

        async def publish(event_name: str, snapshot: dict[str, Any]) -> None:
            payload = TaskUpdatedData.model_validate(snapshot).model_dump()
            await ctx.emit(
                event_name,
                EventContext(
                    event=snapshot,
                    client_event={"type": "task_updated", "data": payload},
                ),
            )

        registry.on_update = lambda snapshot: publish(Events.JOB_UPDATED, snapshot)
        registry.on_complete = lambda snapshot: publish(
            Events.JOB_COMPLETED, snapshot
        )

        def list_tasks(_request: EmptyRequest) -> TaskCatalog:
            return TaskCatalog(tuple(
                task_snapshot(snapshot) for snapshot in registry.snapshots()
            ))

        async def stop_task(request: StopTask) -> StoppedTasks:
            job = registry.get_or_none(request.task_id)
            if job is None:
                raise OperationError(
                    "task_not_found",
                    f"Unknown task: {request.task_id}",
                )
            await registry.cancel(request.task_id)
            return StoppedTasks((task_snapshot(registry.snapshot(job)),))

        async def stop_all(_request: EmptyRequest) -> StoppedTasks:
            return StoppedTasks(tuple(
                task_snapshot(snapshot)
                for snapshot in await registry.stop_all()
            ))

        ctx.on(LIST_TASKS.name, list_tasks)
        ctx.on(STOP_TASK.name, stop_task)
        ctx.on(STOP_ALL_TASKS.name, stop_all)

        def prepare_fork(_request: PrepareFork) -> None:
            if registry.is_busy():
                raise OperationError(
                    "thread_busy",
                    "Cannot fork while a background task is active.",
                    retryable=True,
                )

        ctx.on(PREPARE_FORK, prepare_fork)

        async def close(_event: Any) -> None:
            await registry.shutdown()

        ctx.on(Events.SESSION_CLOSE, close)


plugin = JobsComponent()
