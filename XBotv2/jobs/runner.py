"""Runner contract that adapts a workload to the unified job lifecycle.

A JobRunner implements one concrete job kind (subagent, shell, future remote
worker). It never manages its own status, waiting, cancellation, or storage;
the JobRegistry owns all lifecycle state. ``cancel`` may be called while the
runner is active so the runner can release external resources; the registry
then cancels the runner task itself.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from XBotv2.core.jobs import Job, JobResult
from XBotv2.jobs.output import StreamOutputStore, TextOutputStore


class JobContext:
    """Capabilities a runner uses while executing one job."""

    def __init__(self, registry: Any, job: Job) -> None:
        self.registry = registry
        self.job = job
        self.outputs = _OutputFactory()
        self.primary_output = None

    def set_handle(self, handle: Any) -> None:
        self.job.runtime_handle = handle


class _OutputFactory:
    """Creates job-owned output stores for runners."""

    @staticmethod
    def create_text(text: str = "") -> TextOutputStore:
        return TextOutputStore(text)

    @staticmethod
    def create_stream() -> StreamOutputStore:
        return StreamOutputStore()


class JobRunner(Protocol):
    """Executes one job kind against a JobContext."""

    async def run(self, job: Job, ctx: JobContext) -> JobResult: ...

    async def cancel(self, job: Job) -> None: ...


__all__ = ["JobContext", "JobRunner"]
