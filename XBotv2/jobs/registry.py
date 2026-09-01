"""Kind-agnostic lifecycle registry for runtime jobs.

The registry owns every lifecycle concern shared by all job kinds: ids,
status transitions, waiting, cancellation, event notification, result/output
storage, and cleanup. Domain adapters (shell, subagent) only implement a
JobRunner and their model-facing tools; they never hold job state themselves.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any, Awaitable, Callable

from XBotv2.jobs.contracts import (
    TERMINAL_STATES,
    CancelResult,
    Job,
    JobError,
    JobId,
    JobKind,
    JobNotFound,
    JobRegistryClosed,
    JobResult,
    JobStatus,
    JobSummary,
    MAX_SUMMARY_CHARS,
    WaitResult,
    WaitMode,
    JobRunner,
    TaskSnapshot,
)
from XBotv2.jobs.runner import JobContext
from XBotv2.core.tools import JsonObject

logger = logging.getLogger("xbotv2.jobs")

TaskCallback = Callable[[TaskSnapshot], Awaitable[None]]

# Client-facing kind names preserved for the protocol / TUI task surface.
_PROTOCOL_KIND = {
    JobKind.SUBAGENT: "agent",
    JobKind.SHELL: "shell",
}
_PROTOCOL_STATUS = {
    JobStatus.PENDING: "pending",
    JobStatus.RUNNING: "running",
    JobStatus.COMPLETED: "completed",
    JobStatus.FAILED: "failed",
    JobStatus.CANCELLED: "stopped",
}
_MAX_SNAPSHOT_OUTPUT = 2_000
_MAX_SNAPSHOT_COMMAND = 1_000
_MAX_SUBAGENT_PROMPT_PREVIEW = 100


class JobRegistry:
    """One shared lifecycle store for all jobs owned by an engine."""

    def __init__(
        self,
        *,
        limits: dict[JobKind, int] | None = None,
        prefix: str = "job",
    ) -> None:
        self._jobs: dict[JobId, Job] = {}
        self._completion_events: dict[JobId, asyncio.Event] = {}
        self._runners: dict[JobId, JobRunner] = {}
        self._tasks: dict[JobId, asyncio.Task[None]] = {}
        self._next_id = 1
        self._prefix = prefix
        self._limits: dict[JobKind, asyncio.Semaphore] = {
            kind: asyncio.Semaphore(limit)
            for kind, limit in (limits or {}).items()
        }
        self._closing = False
        # Session wiring hooks; both receive a protocol-facing snapshot dict.
        self.on_update: TaskCallback | None = None
        self.on_complete: TaskCallback | None = None

    @property
    def closing(self) -> bool:
        return self._closing

    # ------------------------------------------------------------------
    # Creation / lifecycle
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        kind: JobKind,
        metadata: JsonObject | None = None,
        parent_job_id: JobId | None = None,
        name: str | None = None,
    ) -> Job:
        """Create one PENDING job and register it in the store."""
        if self._closing:
            raise JobRegistryClosed("session_closing")
        job = Job(
            id=self._next_job_id(kind),
            kind=kind,
            status=JobStatus.PENDING,
            parent_job_id=parent_job_id,
            name=name,
            metadata=dict(metadata or {}),
        )
        self._jobs[job.id] = job
        self._completion_events[job.id] = asyncio.Event()
        await self._notify_update(job)
        return job

    def start(self, job_id: JobId, runner: JobRunner) -> Job:
        """Begin executing *job_id* with *runner* in the background."""
        job = self._require(job_id)
        if job.status is not JobStatus.PENDING:
            raise ValueError(f"Job {job_id} is not pending")
        self._runners[job.id] = runner
        self._tasks[job.id] = asyncio.create_task(
            self._execute(job, runner),
            name=f"xbotv2-{job.id}",
        )
        return job

    def get(self, job_id: JobId) -> Job:
        return self._require(job_id)

    def get_or_none(self, job_id: JobId) -> Job | None:
        return self._jobs.get(job_id)

    def summary(self, job_id: JobId) -> JobSummary:
        return job_summary(self._require(job_id))

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def is_busy(self) -> bool:
        """Whether any job is still pending or running."""
        return any(
            job.status in {JobStatus.PENDING, JobStatus.RUNNING}
            for job in self._jobs.values()
        )

    def list(
        self,
        *,
        kind: JobKind | None = None,
        status: JobStatus | None = None,
        parent_job_id: JobId | None = None,
        recursive: bool = False,
        max_results: int = 20,
    ) -> list[JobSummary]:
        """Return lightweight summaries, newest first, capped by default."""
        items = list(self._jobs.values())
        if kind is not None:
            items = [job for job in items if job.kind is kind]
        if status is not None:
            items = [job for job in items if job.status is status]
        if parent_job_id is not None:
            items = [
                job
                for job in items
                if (recursive and job.parent_job_id is not None)
                or (not recursive and job.parent_job_id == parent_job_id)
            ]
        items.sort(key=lambda job: job.created_at, reverse=True)
        return [job_summary(job) for job in items[:max_results]]

    # ------------------------------------------------------------------
    # Waiting
    # ------------------------------------------------------------------

    async def wait(
        self,
        ids: list[JobId],
        *,
        mode: WaitMode = "all",
        timeout: float | None = None,
    ) -> WaitResult:
        """Wait for job completions and return only lightweight summaries.

        Bulk output is never included; use the domain ``read_*`` tools for text.
        """
        jobs = [self._require(job_id) for job_id in ids]
        if mode == "any":
            return await self._wait_any(jobs, timeout)
        return await self._wait_all(jobs, timeout)

    async def _wait_all(
        self,
        jobs: list[Job],
        timeout: float | None,
    ) -> WaitResult:
        events = [self._completion_events[job.id] for job in jobs]
        timed_out = False
        if events:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(event.wait() for event in events)),
                    timeout,
                )
            except asyncio.TimeoutError:
                timed_out = True
            except asyncio.CancelledError:
                raise
        ready = [job for job in jobs if job.terminal]
        pending = [job.id for job in jobs if not job.terminal]
        return WaitResult(
            ready=[job_summary(job) for job in ready],
            pending=pending,
            timed_out=timed_out,
        )

    async def _wait_any(
        self,
        jobs: list[Job],
        timeout: float | None,
    ) -> WaitResult:
        active = [job for job in jobs if not job.terminal]
        done_tasks = [
            asyncio.create_task(self._completion_events[job.id].wait())
            for job in active
        ]
        timed_out = False
        if done_tasks:
            try:
                done, pending = await asyncio.wait(
                    done_tasks,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                for task in done_tasks:
                    task.cancel()
                raise
            timed_out = not done
            for task in pending:
                task.cancel()
        ready = [job for job in jobs if job.terminal]
        pending = [job.id for job in jobs if not job.terminal]
        return WaitResult(
            ready=[job_summary(job) for job in ready],
            pending=pending,
            timed_out=timed_out,
        )

    # ------------------------------------------------------------------
    # Cancellation / removal / shutdown
    # ------------------------------------------------------------------

    async def cancel(self, job_id: JobId) -> CancelResult:
        """Cancel a job idempotently. Terminal jobs are left untouched."""
        job = self._require(job_id)
        if job.terminal:
            return CancelResult(
                id=job_id, status=job.status.value, cancelled=False
            )
        if job.status is JobStatus.PENDING:
            await self._finish(job, JobStatus.CANCELLED)
            return CancelResult(id=job_id, status=job.status.value, cancelled=True)

        runner_task = self._tasks.get(job.id)
        if runner_task is None or runner_task.done():
            return CancelResult(
                id=job_id, status=job.status.value, cancelled=False
            )
        runner = self._runners.get(job.id)
        if runner is not None:
            try:
                await runner.cancel(job)
            except BaseException:  # noqa: BLE001 - cancellation must proceed
                logger.exception("runner.cancel failed for job %s", job_id)
        runner_task.cancel()
        await asyncio.gather(runner_task, return_exceptions=True)
        return CancelResult(id=job_id, status=job.status.value, cancelled=True)

    def remove(self, job_id: JobId) -> None:
        """Drop one terminal job and its outputs from the registry."""
        job = self._jobs.pop(job_id, None)
        if job is None:
            return
        self._completion_events.pop(job_id, None)
        self._runners.pop(job_id, None)
        self._tasks.pop(job_id, None)
        job.result = None
        job.metadata.clear()

    async def stop_all(self) -> list[TaskSnapshot]:
        """Cancel every non-terminal job and return their final snapshots."""
        active = [job for job in self._jobs.values() if not job.terminal]
        for job in active:
            await self.cancel(job.id)
        return [self.snapshot(job) for job in active]

    async def shutdown(self) -> list[TaskSnapshot]:
        """Cancel all non-terminal jobs; drop terminal jobs' outputs."""
        self._closing = True
        # Suppress completion notices: shutdown is not an ordinary completion.
        self.on_update = None
        self.on_complete = None
        snapshots = await self.stop_all()
        self.remove_all()
        return snapshots

    def remove_all(self) -> None:
        for job_id in list(self._jobs):
            self.remove(job_id)

    # ------------------------------------------------------------------
    # Execution wrapper
    # ------------------------------------------------------------------

    async def _execute(self, job: Job, runner: JobRunner) -> None:
        ctx = JobContext()
        semaphore = self._limits.get(job.kind)
        acquired = False
        try:
            if semaphore is not None:
                await semaphore.acquire()
                acquired = True
            job.status = JobStatus.RUNNING
            job.started_at = time.time()
            await self._notify_update(job)
            result = await runner.run(job, ctx)
            job.result = result
            job.status = JobStatus.COMPLETED
        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
        except Exception as exc:  # noqa: BLE001 - job failures are state
            job.error = normalize_error(exc)
            job.status = JobStatus.FAILED
        finally:
            if semaphore is not None and acquired:
                semaphore.release()
            if job.result is None and ctx.primary_output is not None:
                job.result = JobResult(output_store=ctx.primary_output)
            await self._finish(job, job.status)

    async def _finish(self, job: Job, status: JobStatus) -> None:
        job.status = status
        job.finished_at = time.time()
        # A completion waiter observes the fully published terminal transition,
        # not merely the runner having stopped.
        await self._notify_update(job)
        await self._notify_complete(job)
        event = self._completion_events.get(job.id)
        if event is not None:
            event.set()

    # ------------------------------------------------------------------
    # Notification
    # ------------------------------------------------------------------

    async def _notify_update(self, job: Job) -> None:
        if self.on_update is None:
            return
        try:
            await self.on_update(self.snapshot(job))
        except BaseException:  # noqa: BLE001 - notify must not break jobs
            logger.exception("job on_update hook failed for %s", job.id)

    async def _notify_complete(self, job: Job) -> None:
        if self.on_complete is None:
            return
        try:
            await self.on_complete(
                self.snapshot(job, full_output=(job.kind is JobKind.SUBAGENT))
            )
        except BaseException:  # noqa: BLE001 - notify must not break cleanup
            logger.exception("job on_complete hook failed for %s", job.id)

    # ------------------------------------------------------------------
    # Snapshot / rendering
    # ------------------------------------------------------------------

    def snapshot(self, job: Job, *, full_output: bool = False) -> TaskSnapshot:
        """Build one bounded client-facing task snapshot."""
        metadata = job.metadata
        if job.kind is JobKind.SHELL:
            command = str(metadata.get("command") or "")
            cwd = str(metadata.get("cwd") or "")
            agent = ""
            thread_id = ""
        else:
            command = str(metadata.get("command") or "")
            cwd = ""
            agent = str(metadata.get("agent") or "")
            thread_id = str(metadata.get("thread_id") or "")
        output = self._snapshot_output(job, full_output=full_output)
        error = str(job.error.message if job.error is not None else "")
        return TaskSnapshot(
            task_id=job.id,
            kind=_PROTOCOL_KIND[job.kind],
            command=command if full_output else _preview(command, _MAX_SNAPSHOT_COMMAND),
            cwd=cwd,
            status=_PROTOCOL_STATUS[job.status],
            created_at=job.created_at,
            started_at=job.started_at or 0.0,
            finished_at=job.finished_at or 0.0,
            output=output,
            error=_preview(error, _MAX_SNAPSHOT_OUTPUT),
            agent=agent,
            thread_id=thread_id,
            usage=dict(
                (job.result.data.get("usage") if job.result is not None else {}) or {}
            ),
        )

    def snapshots(self) -> list[TaskSnapshot]:
        """Snapshot every live job in registration order."""
        return [self.snapshot(job) for job in self.all()]

    @staticmethod
    def _snapshot_output(job: Job, *, full_output: bool) -> str:
        result = job.result
        if result is None or result.output_store is None:
            return ""
        text = getattr(result.output_store, "all", None)
        if text is None:
            return ""
        value = text()
        return value if full_output else _preview(value, _MAX_SNAPSHOT_OUTPUT)

    def _next_job_id(self, kind: JobKind) -> JobId:
        prefix = "sa" if kind is JobKind.SUBAGENT else "sh"
        while True:
            job_id = f"{prefix}_{self._next_id}"
            self._next_id += 1
            if job_id not in self._jobs:
                return job_id

    def _require(self, job_id: JobId) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound(job_id)
        return job


def job_summary(job: Job) -> JobSummary:
    """Build the lightweight model-facing summary for one job."""
    result = job.result
    summary = None
    if result is not None and result.summary:
        summary = _preview(result.summary, MAX_SUMMARY_CHARS)
    elif job.error is not None:
        summary = _preview(job.error.message, MAX_SUMMARY_CHARS)
    return JobSummary(
        id=job.id,
        kind=job.kind.value,
        status=job.status.value,
        name=job.name,
        elapsed_ms=job.elapsed_ms,
        parent_job_id=job.parent_job_id,
        summary=summary,
    )


def normalize_error(exc: BaseException) -> JobError:
    """Map an exception to a structured, model-safe JobError."""
    code = getattr(exc, "job_error_code", None)
    if isinstance(code, str):
        return JobError(code=code, message=str(exc))
    return JobError(
        code=getattr(exc, "code", None) or "job_failed",
        message=str(exc) or type(exc).__name__,
    )


def _preview(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n[truncated; {len(value) - limit} characters omitted]"


__all__ = [
    "JobRegistry",
    "job_summary",
    "normalize_error",
]
