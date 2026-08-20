"""Jobs component: the background job registry as an XCore service.

The plugin owns registry limits, lifecycle notifications, cancellation, and
output storage. Domain adapters (subagents and shell) implement ``JobRunner``.
"""

from __future__ import annotations

import json
from typing import Any, cast

from XBotv2.application import RUNTIME_EVENT, RuntimeEvent
from XBotv2.core.errors import OperationError
from XBotv2.agentloop import AgentLoopDriverPort, Events
from XBotv2.core.prompts import prompt_container, prompt_element
from XBotv2.jobs import JobKind
from XBotv2.jobs.commands import build_jobs_commands
from XBotv2.jobs.protocol import task_completion_event, task_updated_event
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
from XBotv2.session import PREPARE_FORK, PrepareFork


class JobsComponent:
    inject = {
        "required": ["commands"],
        "optional": ["engine"],
    }
    """Register the job registry as ``ctx.jobs``."""

    name = "xbot.jobs"

    def apply(self, ctx: Any, config: Any = None) -> None:
        max_concurrent = int((config or {}).get("max_concurrent_subagents", 4))
        registry = JobRegistry(limits={JobKind.SUBAGENT: max_concurrent})
        ctx.set("jobs", registry)
        for command in build_jobs_commands(registry):
            ctx.commands.register(command)

        async def publish_update(snapshot: dict[str, Any]) -> None:
            await ctx.emit(
                RUNTIME_EVENT,
                RuntimeEvent(client_event=task_updated_event(
                    task_snapshot(snapshot)
                )),
            )

        async def publish_completion(snapshot: dict[str, Any]) -> None:
            task = task_snapshot(snapshot)
            event = task_completion_event(task)
            engine = cast(
                AgentLoopDriverPort | None,
                ctx.get("engine", strict=False),
            )
            if engine is not None:
                payload = event.data
                await engine.inject(
                    prompt_container(
                        "runtime_event",
                        [prompt_element(
                            "payload",
                            json.dumps(payload, ensure_ascii=False, sort_keys=True),
                            attributes={"encoding": "json"},
                        )],
                        attributes={"source": "tasks", "event": "completed"},
                    ),
                    source=task.task_id,
                    metadata={"kind": "notification", "payload": payload},
                )
            await ctx.emit(
                RUNTIME_EVENT,
                RuntimeEvent(client_event=event),
            )

        registry.on_update = publish_update
        registry.on_complete = publish_completion

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
