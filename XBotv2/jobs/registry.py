"""Lifecycle registry for owner-defined jobs."""

from __future__ import annotations

import asyncio
import logging
import time

from XBotv2.jobs.contracts import (
    JOB_COMPLETED,
    JOB_UPDATED,
    CancelResult,
    CancelledBeforeStart,
    CancelledRunning,
    FailedBeforeStart,
    FailedRunning,
    Job,
    JobError,
    JobEventPort,
    JobId,
    JobIdentity,
    JobNotFound,
    JobRegistryClosed,
    JobRunner,
    JobStateName,
    JobSpec,
    JobView,
    JobsPort,
    MAX_SUMMARY_CHARS,
    Queued,
    Running,
    Succeeded,
    WaitMode,
    WaitResult,
)

logger = logging.getLogger("xbotv2.jobs")


class JobRegistry(JobsPort):
    def __init__(
        self,
        *,
        limits: dict[str, int] | None = None,
        prefix: str = "job",
        publisher: JobEventPort | None = None,
    ) -> None:
        self._jobs: dict[JobId, Job] = {}
        self._completion_events: dict[JobId, asyncio.Event] = {}
        self._runners: dict[JobId, JobRunner] = {}
        self._tasks: dict[JobId, asyncio.Task[None]] = {}
        self._next_id = 1
        self._prefix = prefix
        self._limits = {
            kind: asyncio.Semaphore(limit) for kind, limit in (limits or {}).items()
        }
        self._closing = False
        self._publisher = publisher

    @property
    def closing(self) -> bool:
        return self._closing

    async def create(
        self,
        *,
        spec: JobSpec,
        owner: str,
        parent_job_id: JobId | None = None,
        name: str | None = None,
    ) -> Job:
        if self._closing:
            raise JobRegistryClosed("session_closing")
        if not owner.strip() or not spec.kind.strip() or not spec.label.strip():
            raise ValueError("job owner, kind, and label are required")
        job = Job(
            identity=JobIdentity(
                id=self._next_job_id(spec.kind),
                owner=owner,
                parent=parent_job_id,
                name=name,
                created_at=time.time(),
            ),
            spec=spec,
        )
        self._jobs[job.id] = job
        self._completion_events[job.id] = asyncio.Event()
        await self._notify(job)
        return job

    def start(self, job_id: JobId, runner: JobRunner) -> Job:
        job = self._require(job_id)
        if not isinstance(job.state, Queued):
            raise ValueError(f"Job {job_id} is not queued")
        self._runners[job_id] = runner
        self._tasks[job_id] = asyncio.create_task(
            self._execute(job, runner), name=f"xbotv2-{job_id}"
        )
        return job

    def get_or_none(self, job_id: JobId) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def views(self) -> list[JobView]:
        return [self.view(job) for job in self._jobs.values()]

    def is_busy(self) -> bool:
        return any(not job.terminal for job in self._jobs.values())

    def list(
        self,
        *,
        kind: str | None = None,
        status: JobStateName | None = None,
        parent_job_id: JobId | None = None,
        recursive: bool = False,
        max_results: int = 20,
    ) -> list[JobView]:
        jobs = list(self._jobs.values())
        if kind is not None:
            jobs = [job for job in jobs if job.kind == kind]
        if status is not None:
            jobs = [job for job in jobs if job.status == status]
        if parent_job_id is not None:
            jobs = [
                job for job in jobs
                if (recursive and job.parent_job_id is not None)
                or (not recursive and job.parent_job_id == parent_job_id)
            ]
        jobs.sort(key=lambda job: job.created_at, reverse=True)
        return [self.view(job) for job in jobs[:max_results]]

    async def wait(
        self,
        ids: list[JobId],
        *,
        mode: WaitMode = "all",
        timeout: float | None = None,
    ) -> WaitResult:
        jobs = [self._require(job_id) for job_id in ids]
        active = [job for job in jobs if not job.terminal]
        timed_out = False
        waits = [self._completion_events[job.id].wait() for job in active]
        if waits:
            try:
                if mode == "all":
                    await asyncio.wait_for(asyncio.gather(*waits), timeout)
                else:
                    tasks = [asyncio.create_task(wait) for wait in waits]
                    done, pending = await asyncio.wait(
                        tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                    )
                    timed_out = not done
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
            except asyncio.TimeoutError:
                timed_out = True
        return WaitResult(
            ready=tuple(self.view(job) for job in jobs if job.terminal),
            pending=tuple(job.id for job in jobs if not job.terminal),
            timed_out=timed_out,
        )

    async def cancel(self, job_id: JobId) -> CancelResult:
        job = self._require(job_id)
        if job.terminal:
            return CancelResult(job_id, job.status, False)
        task = self._tasks.get(job_id)
        runner = self._runners.get(job_id)
        if runner is not None:
            try:
                await runner.cancel(job)
            except BaseException:
                logger.exception("runner.cancel failed for job %s", job_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        cancelled = isinstance(job.state, (CancelledBeforeStart, CancelledRunning))
        return CancelResult(job_id, job.status, cancelled)

    async def stop_all(self) -> list[JobView]:
        active = [job for job in self._jobs.values() if not job.terminal]
        await asyncio.gather(*(self.cancel(job.id) for job in active), return_exceptions=True)
        return [self.view(job) for job in active]

    async def shutdown(self) -> list[JobView]:
        self._closing = True
        views = await self.stop_all()
        self.remove_all()
        return views

    def remove(self, job_id: JobId) -> None:
        self._jobs.pop(job_id, None)
        self._completion_events.pop(job_id, None)
        self._runners.pop(job_id, None)
        self._tasks.pop(job_id, None)

    def remove_all(self) -> None:
        for job_id in tuple(self._jobs):
            self.remove(job_id)

    async def _execute(self, job: Job, runner: JobRunner) -> None:
        semaphore = self._limits.get(job.kind)
        acquired = False
        try:
            if semaphore is not None:
                await semaphore.acquire()
                acquired = True
            if job.terminal:
                return
            started = time.time()
            job.state = Running(started)
            await self._notify(job)
            result = await runner.run(job)
            job.state = Succeeded(started, time.time(), result)
        except asyncio.CancelledError:
            now = time.time()
            if isinstance(job.state, Running):
                job.state = CancelledRunning(job.state.started_at, now, "cancelled")
            else:
                job.state = CancelledBeforeStart(now, "cancelled")
        except Exception as exc:
            now = time.time()
            error = normalize_error(exc)
            if isinstance(job.state, Running):
                job.state = FailedRunning(job.state.started_at, now, error)
            else:
                job.state = FailedBeforeStart(now, error)
        finally:
            if acquired and semaphore is not None:
                semaphore.release()
            await self._notify(job)
            if job.terminal:
                event = self._completion_events.get(job.id)
                if event is not None:
                    event.set()
                await self._notify_complete(job)

    async def _notify(self, job: Job) -> None:
        if self._publisher is None or self._closing:
            return
        try:
            await self._publisher.emit(JOB_UPDATED, self.view(job))
        except BaseException:
            logger.exception("job update publish failed for %s", job.id)

    async def _notify_complete(self, job: Job) -> None:
        if self._publisher is None or self._closing:
            return
        try:
            await self._publisher.emit(JOB_COMPLETED, self.view(job))
        except BaseException:
            logger.exception("job completion publish failed for %s", job.id)

    @staticmethod
    def view(job: Job) -> JobView:
        summary = job.error.message if job.error is not None else None
        return JobView(
            id=job.id,
            kind=job.kind,
            label=job.spec.label,
            state=job.status,
            elapsed_ms=job.elapsed_ms,
            summary=_preview(summary, MAX_SUMMARY_CHARS) if summary else None,
        )

    def _next_job_id(self, kind: str) -> JobId:
        prefix = kind.replace("-", "_")[:8] or self._prefix
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


def normalize_error(exc: BaseException) -> JobError:
    code = getattr(exc, "job_error_code", None) or getattr(exc, "code", None) or "job_failed"
    return JobError(code=str(code), message=str(exc) or type(exc).__name__)


def _preview(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[:limit]}\n[truncated]"


__all__ = ["JobRegistry", "normalize_error"]
