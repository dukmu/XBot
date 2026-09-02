"""Jobs component: the background job registry as an XCore service.

The plugin owns registry limits, lifecycle notifications, cancellation, and
output storage. Domain adapters (subagents and shell) implement ``JobRunner``.
"""

from __future__ import annotations

import json
from typing import Any

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
    TaskSnapshot,
)
from XBotv2.session.contracts import PREPARE_FORK, PrepareFork


class JobsComponent:
    inject = {
        "required": ["commands", "engine"],
    }
    """Register the job registry as ``ctx.jobs``."""

    name = "xbot.jobs"

    def apply(self, ctx: Any, config: Any = None) -> None:
        max_concurrent = int((config or {}).get("max_concurrent_subagents", 4))
        registry = JobRegistry(limits={JobKind.SUBAGENT: max_concurrent})
        ctx.set("jobs", registry)
        for command in build_jobs_commands(registry):
            ctx.commands.register(command)
        handlers = JobHandlers(registry, ctx.engine, ctx)
        registry.on_update = handlers.publish_update
        registry.on_complete = handlers.publish_completion
        ctx.on(LIST_TASKS.name, handlers.list_tasks)
        ctx.on(STOP_TASK.name, handlers.stop_task)
        ctx.on(STOP_ALL_TASKS.name, handlers.stop_all)
        ctx.on(PREPARE_FORK, handlers.prepare_fork)
        ctx.on(Events.SESSION_CLOSE, handlers.close)


class JobHandlers:
    def __init__(
        self,
        registry: JobRegistry,
        engine: AgentLoopDriverPort,
        events: Any,
    ) -> None:
        self._registry = registry
        self._engine = engine
        self._events = events

    async def publish_update(self, snapshot: TaskSnapshot) -> None:
        await self._events.emit(
            RUNTIME_EVENT,
            RuntimeEvent(client_event=task_updated_event(snapshot)),
        )

    async def publish_completion(self, snapshot: TaskSnapshot) -> None:
        event = task_completion_event(snapshot)
        payload = event.data
        await self._engine.inject(
            prompt_container(
                "runtime_event",
                [prompt_element(
                    "payload",
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    attributes={"encoding": "json"},
                )],
                attributes={"source": "tasks", "event": "completed"},
            ),
            source=snapshot.task_id,
            metadata={"kind": "notification", "payload": payload},
        )
        await self._events.emit(
            RUNTIME_EVENT,
            RuntimeEvent(client_event=event),
        )

    def list_tasks(self, _request: EmptyRequest) -> TaskCatalog:
        return TaskCatalog(tuple(self._registry.snapshots()))

    async def stop_task(self, request: StopTask) -> StoppedTasks:
        job = self._registry.get_or_none(request.task_id)
        if job is None:
            raise OperationError("task_not_found", f"Unknown task: {request.task_id}")
        await self._registry.cancel(request.task_id)
        return StoppedTasks((self._registry.snapshot(job),))

    async def stop_all(self, _request: EmptyRequest) -> StoppedTasks:
        return StoppedTasks(tuple(await self._registry.stop_all()))

    def prepare_fork(self, _request: PrepareFork) -> None:
        if self._registry.is_busy():
            raise OperationError(
                "thread_busy",
                "Cannot fork while a background task is active.",
                retryable=True,
            )

    async def close(self, _event: Any) -> None:
        await self._registry.shutdown()


plugin = JobsComponent()
