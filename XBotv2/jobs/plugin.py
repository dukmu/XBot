"""Jobs component: the background job registry as an XCore service.

The plugin owns registry limits, lifecycle notifications, cancellation, and
output storage. Domain adapters (subagents and shell) implement ``JobRunner``.
"""

from __future__ import annotations

import json
from xcore import Context

from XBotv2.application import RUNTIME_EVENT, RuntimeEvent
from XBotv2.core.errors import OperationError
from XBotv2.agentloop import (
    AgentLoopDriverPort,
    EventPort,
    Events,
    InboxItem,
    InboxTarget,
    RuntimeInput,
)
from XBotv2.agentloop.events import SessionLifecycle
from XBotv2.core.prompts import prompt_container, prompt_element
from XBotv2.jobs.commands import build_jobs_commands
from XBotv2.jobs.contracts import JOB_COMPLETED, JOB_UPDATED
from XBotv2.jobs.protocol import (
    build_jobs_router,
    job_completed_event,
    job_updated_event,
)
from XBotv2.jobs.registry import JobRegistry
from XBotv2.core.operations import EmptyRequest
from XBotv2.jobs.contracts import (
    JobsConfig,
    LIST_JOBS,
    STOP_ALL_JOBS,
    STOP_JOB,
    StopJob,
    StoppedJobs,
    JobCatalog,
    JobView,
)
from XBotv2.session.contracts import PREPARE_FORK, PrepareFork
from XBotv2.server import contribute_router


async def mount_http(ctx: Context) -> None:
    await contribute_router(
        ctx,
        owner="xbot.jobs.http",
        router=build_jobs_router(sessions=ctx.sessions),
    )


class JobsRuntimeComponent:
    inject = {
        "required": ["commands", "engine"],
    }
    """Register the job registry as ``ctx.jobs``."""

    name = "xbot.jobs"
    Config = JobsConfig

    def apply(self, ctx: Context, config: JobsConfig) -> None:
        max_concurrent = config.max_concurrent_subagents
        # The registry publishes lifecycle transitions on the bus; this
        # component subscribes with fiber-owned listeners, so notifications
        # are delivered whenever the registry runs — never by assignment
        # order inside apply.
        registry = JobRegistry(
            limits={"subagent": max_concurrent},
            publisher=ctx,
        )
        ctx.set("jobs", registry)
        for command in build_jobs_commands(registry):
            ctx.commands.register(command)
        handlers = JobHandlers(registry, ctx.engine, ctx)
        ctx.on(JOB_UPDATED, handlers.publish_update)
        ctx.on(JOB_COMPLETED, handlers.publish_completion)
        ctx.on(LIST_JOBS.name, handlers.list_jobs)
        ctx.on(STOP_JOB.name, handlers.stop_job)
        ctx.on(STOP_ALL_JOBS.name, handlers.stop_all)
        ctx.on(PREPARE_FORK, handlers.prepare_fork)
        ctx.on(Events.SESSION_CLOSE, handlers.close)


class JobHandlers:
    def __init__(
        self,
        registry: JobRegistry,
        engine: AgentLoopDriverPort,
        events: EventPort,
    ) -> None:
        self._registry = registry
        self._engine = engine
        self._events = events

    async def publish_update(self, view: JobView) -> None:
        await self._events.emit(
            RUNTIME_EVENT,
            RuntimeEvent(event=job_updated_event(view)),
        )

    async def publish_completion(self, view: JobView) -> None:
        event = job_completed_event(view)
        payload = event.model_dump(mode="json")
        await self._engine.submit_input(
            InboxItem(
                target=InboxTarget.NEXT_STEP,
                input=RuntimeInput(
                    source=view.id,
                    event="completed",
                    content=prompt_container(
                        "runtime_event",
                        [prompt_element(
                            "payload",
                            json.dumps(payload, ensure_ascii=False, sort_keys=True),
                            attributes={"encoding": "json"},
                        )],
                        attributes={"source": "jobs", "event": "completed"},
                    ),
                ),
            ),
            wake=False,
        )
        await self._events.emit(
            RUNTIME_EVENT,
            RuntimeEvent(event=event),
        )

    def list_jobs(self, _request: EmptyRequest) -> JobCatalog:
        return JobCatalog(tuple(self._registry.views()))

    async def stop_job(self, request: StopJob) -> StoppedJobs:
        job = self._registry.get_or_none(request.job_id)
        if job is None:
            raise OperationError("job_not_found", f"Unknown job: {request.job_id}")
        await self._registry.cancel(request.job_id)
        return StoppedJobs((self._registry.view(job),))

    async def stop_all(self, _request: EmptyRequest) -> StoppedJobs:
        return StoppedJobs(tuple(await self._registry.stop_all()))

    def prepare_fork(self, _request: PrepareFork) -> None:
        if self._registry.is_busy():
            raise OperationError(
                "thread_busy",
                "Cannot fork while a background task is active.",
                retryable=True,
            )

    async def close(self, _event: SessionLifecycle) -> None:
        await self._registry.shutdown()


class JobsPlugin:
    """Compose Agent job execution and its process HTTP projection."""

    name = "xbot.jobs"
    Config = JobsConfig

    async def apply(self, ctx: Context, config: JobsConfig) -> None:
        await ctx.plugin(JobsRuntimeComponent(), config)
        await ctx.inject(["server", "sessions"], mount_http)


plugin = JobsPlugin()

__all__ = ["JobsPlugin"]
