"""Jobs component: the background job registry as an XCore service.

The registry is created by this plugin (mounted after the session component,
whose ``ctx.runtime`` supplies the concurrency limits) and provided as
``ctx.jobs``.  Job lifecycle — waiting, cancellation, output storage — lives
in the registry; domain adapters (subagents, shell) implement ``JobRunner``.
"""

from __future__ import annotations

from typing import Any

from XBotv2.jobs.model import JobKind
from XBotv2.jobs.registry import JobRegistry


class JobsComponent:
    """Register the job registry as ``ctx.jobs``."""

    name = "xbot.jobs"

    def apply(self, ctx: Any, config: Any = None) -> None:
        runtime_config = ctx.runtime
        ctx.set(
            "jobs",
            JobRegistry(
                limits={
                    JobKind.SUBAGENT: runtime_config.max_concurrent_subagents,
                },
            ),
        )


plugin = JobsComponent()
