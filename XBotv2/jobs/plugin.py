"""Jobs component: the background job registry as an XCore service.

The registry is created by this plugin (mounted after the session component,
whose ``ctx.runtime`` supplies the concurrency limits) and provided as
``ctx.jobs``.  Job lifecycle — waiting, cancellation, output storage — lives
in the registry; domain adapters (subagents, shell) implement ``JobRunner``.
"""

from __future__ import annotations

from typing import Any

from XBotv2.core.jobs import JobKind
from XBotv2.jobs.registry import JobRegistry
from XBotv2.core.events import EventContext, Events


class JobsComponent:
    inject = ['session']
    """Register the job registry as ``ctx.jobs``."""

    name = "xbot.jobs"

    def apply(self, ctx: Any, config: Any = None) -> None:
        max_concurrent = int((config or {}).get("max_concurrent_subagents", 4))
        registry = JobRegistry(limits={JobKind.SUBAGENT: max_concurrent})
        ctx.set("jobs", registry)

        async def publish(event_name: str, snapshot: dict[str, Any]) -> None:
            await ctx.emit(event_name, EventContext(event=snapshot))

        registry.on_update = lambda snapshot: publish(Events.JOB_UPDATED, snapshot)
        registry.on_complete = lambda snapshot: publish(
            Events.JOB_COMPLETED, snapshot
        )

        async def close(_event: Any) -> None:
            await registry.shutdown()

        ctx.on(Events.SESSION_CLOSE, close)


plugin = JobsComponent()
