"""Lifecycle registry for owner-defined jobs."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from XBotv2.jobs.api.contracts import (
    JobExecutionError,
    JOB_COMPLETED,
    JOB_UPDATED,
    CancelResult,
    CancelledBeforeStart,
    CancelledRunning,
    FailedBeforeStart,
    FailedRunning,
    Job,
    JobError,
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
from xcore.api.events import EventEmitter
from XBotv2.agentloop.api.wait import wait_for_targets

logger = logging.getLogger("xbotv2.jobs")
_ALL_SCOPES = object()

@dataclass(frozen=True, eq=False, slots=True)
class _Binding:
    key: object


@dataclass(frozen=True, slots=True)
class _CompletedSpec:
    kind: str
    label: str


class JobRegistry(JobsPort):
    def __init__(
        self,
        *,
        limits: dict[str, int] | None = None,
        prefix: str = "job",
        publisher: EventEmitter | None = None,
        completed_limit: int = 100,
        completed_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if completed_limit < 1 or completed_bytes < 1:
            raise ValueError("completed job budgets must be positive")
        self._jobs: dict[JobId, Job] = {}
        self._completion_events: dict[JobId, asyncio.Event] = {}
        self._completed: OrderedDict[JobId, int] = OrderedDict()
        self._completed_size = 0
        self._completed_limit = completed_limit
        self._completed_bytes = completed_bytes
        self._waiters: dict[JobId, int] = {}
        self._cancel_pending = set()
        self._runners: dict[JobId, JobRunner] = {}
        self._tasks: dict[JobId, asyncio.Task[None]] = {}
        self._next_id = 1
        self._prefix = prefix
        self._limits = {
            kind: asyncio.Semaphore(limit) for kind, limit in (limits or {}).items()
        }
        self._closing = False
        self._publisher = publisher
        self._scopes = {}
        self._bindings = {}
        self._publishers = {}
        self._closing_scopes = set()

    def bind(self, key, publisher, inbox=None):
        if self._closing or key in self._bindings:
            raise JobRegistryClosed("job owner is already bound or closing")
        binding = _Binding(key)
        self._bindings[key] = binding
        self._publishers[binding] = publisher
        return ThreadJobs(self, binding, inbox)

    def _authorize(self, scope):
        if scope is _ALL_SCOPES or scope is None:
            return
        if self._bindings.get(scope.key) is not scope:
            raise JobRegistryClosed("thread owner has closed")

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
        scope=None,
    ) -> Job:
        self._authorize(scope)
        if self._closing or scope in self._closing_scopes:
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
        self._scopes[job.id] = scope
        self._completion_events[job.id] = asyncio.Event()
        await self._notify(job)
        return job

    def start(self, job_id: JobId, runner: JobRunner, *, scope=_ALL_SCOPES) -> Job:
        job = self._require(job_id, scope=scope)
        if self._closing or self._scopes.get(job_id) in self._closing_scopes:
            raise JobRegistryClosed("thread_closing")
        if not isinstance(job.state, Queued):
            raise ValueError(f"Job {job_id} is not queued")
        self._runners[job_id] = runner
        self._tasks[job_id] = asyncio.create_task(
            self._execute(job, runner), name=f"xbotv2-{job_id}"
        )
        return job

    def get_or_none(self, job_id: JobId, *, scope=_ALL_SCOPES) -> Job | None:
        self._authorize(scope)
        job = self._jobs.get(job_id)
        if job is None or (scope is not _ALL_SCOPES and self._scopes.get(job_id) != scope):
            return None
        if job_id in self._completed:
            self._completed.move_to_end(job_id)
        return job

    def all(self, *, scope=_ALL_SCOPES) -> list[Job]:
        self._authorize(scope)
        return [job for job in self._jobs.values() if scope is _ALL_SCOPES or self._scopes.get(job.id) == scope]

    def views(self, *, scope=_ALL_SCOPES) -> list[JobView]:
        return [self.view(job, scope=scope) for job in self.all(scope=scope)]

    def is_busy(self, *, scope=_ALL_SCOPES) -> bool:
        return any(not job.terminal for job in self.all(scope=scope))

    def list(
        self,
        *,
        kind: str | None = None,
        status: JobStateName | None = None,
        parent_job_id: JobId | None = None,
        recursive: bool = False,
        max_results: int = 20,
        scope=_ALL_SCOPES,
    ) -> list[JobView]:
        jobs = self.all(scope=scope)
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
        ids: list[JobId] | None = None,
        *,
        mode: WaitMode = "any",
        timeout: float | None = None,
        scope=_ALL_SCOPES,
        inbox=None,
        kind: str | None = None,
    ) -> WaitResult:
        if mode not in {"any", "all"}:
            raise ValueError("mode must be 'any' or 'all'")
        if not ids:
            ids = [job.id for job in self.all(scope=scope) if not job.terminal and (kind is None or job.kind == kind)]
        jobs = [self._require(job_id, scope=scope) for job_id in dict.fromkeys(ids)]
        if kind is not None and any(job.kind != kind for job in jobs):
            raise JobNotFound("Job is not a " + kind)
        for job in jobs:
            self._waiters[job.id] = self._waiters.get(job.id, 0) + 1
        try:
            targets = {
                job.id: (lambda job=job: job.terminal, self._completion_events.get(job.id) or asyncio.Event())
                for job in jobs
            }
            reason = await wait_for_targets(
                targets, mode=mode, timeout=timeout, inbox=inbox,
                completion_event="job/completed",
            )
            return WaitResult(
                ready=tuple(self.view(job) for job in jobs if job.terminal),
                pending=tuple(job.id for job in jobs if not job.terminal),
                timed_out=reason == "timeout",
                results=tuple((job.id, job.result) for job in jobs if job.terminal),
                reason=reason,
            )
        finally:
            for job in jobs:
                self._waiters[job.id] -= 1
                if not self._waiters[job.id]:
                    del self._waiters[job.id]
            self._evict_completed()

    async def cancel(self, job_id: JobId, *, scope=_ALL_SCOPES) -> CancelResult:
        job = self._require(job_id, scope=scope)
        if job.terminal and job_id not in self._cancel_pending:
            return CancelResult(job_id, job.status, False)
        task = self._tasks.get(job_id)
        runner = self._runners.get(job_id)
        self._cancel_pending.add(job_id)
        if task is not None and not task.done():
            task.cancel()
        try:
            if runner is not None:
                await runner.cancel(job)
        finally:
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        self._cancel_pending.discard(job_id)
        # A task cancelled before its coroutine is first scheduled never
        # enters _execute, so its exception handler/finally block cannot
        # publish a terminal state or release waiters.  Cancellation owns
        # that narrow pre-start transition after the task is known done.
        if not job.terminal:
            now = time.time()
            if isinstance(job.state, Running):
                job.state = CancelledRunning(job.state.started_at, now, "cancelled")
            else:
                job.state = CancelledBeforeStart(now, "cancelled")
            await self._notify(job)
            await self._complete(job)
        cancelled = isinstance(job.state, (CancelledBeforeStart, CancelledRunning))
        return CancelResult(job_id, job.status, cancelled)

    async def stop_all(self, *, scope=_ALL_SCOPES) -> list[JobView]:
        active = [job for job in self.all(scope=scope) if not job.terminal or job.id in self._cancel_pending]
        await asyncio.gather(*(self.cancel(job.id, scope=scope) for job in active))
        return [self.view(job) for job in active]

    async def shutdown(self) -> list[JobView]:
        self._closing = True
        views = await self.stop_all()
        self.remove_all()
        self._bindings.clear()
        self._publishers.clear()
        return views

    def remove(self, job_id: JobId, *, scope=_ALL_SCOPES) -> None:
        if scope is not _ALL_SCOPES:
            self._require(job_id, scope=scope)
        self._jobs.pop(job_id, None)
        self._scopes.pop(job_id, None)
        self._completion_events.pop(job_id, None)
        self._runners.pop(job_id, None)
        self._tasks.pop(job_id, None)
        self._completed_size -= self._completed.pop(job_id, 0)

    def remove_all(self, *, scope=_ALL_SCOPES) -> None:
        for job in self.all(scope=scope):
            self.remove(job.id, scope=scope)

    async def shutdown_scope(self, scope):
        if self._bindings.get(scope.key) is not scope:
            return []
        self._closing_scopes.add(scope)
        views = await self.stop_all(scope=scope)
        self.remove_all(scope=scope)
        self._publishers.pop(scope, None)
        self._bindings.pop(scope.key, None)
        self._closing_scopes.discard(scope)
        return views

    async def _execute(self, job: Job, runner: JobRunner) -> None:
        semaphore = self._limits.get(job.kind)
        acquired = False
        result_bytes = 0
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
            result_bytes = _result_bytes(result)
            job.state = Succeeded(started, time.time(), result)
        except asyncio.CancelledError:
            now = time.time()
            if isinstance(job.state, Running):
                job.state = CancelledRunning(job.state.started_at, now, "cancelled")
            else:
                job.state = CancelledBeforeStart(now, "cancelled")
        except Exception as exc:
            now = time.time()
            result = exc.result if isinstance(exc, JobExecutionError) else None
            try:
                result_bytes = _result_bytes(result)
            except Exception as invalid_result:
                exc = invalid_result
                result = None
            error = normalize_error(exc)
            if isinstance(job.state, Running):
                job.state = FailedRunning(
                    job.state.started_at, now, error,
                    result=result,
                )
            else:
                job.state = FailedBeforeStart(now, error)
        finally:
            if acquired and semaphore is not None:
                semaphore.release()
            await self._notify(job)
            if job.terminal:
                await self._complete(job, result_bytes=result_bytes)

    async def _complete(self, job: Job, *, result_bytes: int = 0) -> None:
        job.spec = _CompletedSpec(job.kind, _preview(job.spec.label, MAX_SUMMARY_CHARS))
        event = self._completion_events.pop(job.id, None)
        if event is not None:
            event.set()
        await self._notify_complete(job)
        self._runners.pop(job.id, None)
        self._tasks.pop(job.id, None)
        size = _completed_bytes(job) + result_bytes
        self._completed[job.id] = size
        self._completed_size += size
        self._evict_completed()

    def _evict_completed(self) -> None:
        while len(self._completed) > self._completed_limit or self._completed_size > self._completed_bytes:
            evict = next((job_id for job_id in self._completed if job_id not in self._waiters), None)
            if evict is None:
                break
            self.remove(evict)

    async def _notify(self, job: Job) -> None:
        publisher = self._publishers.get(self._scopes.get(job.id), self._publisher)
        if publisher is None or self._closing or self._scopes.get(job.id) in self._closing_scopes:
            return
        try:
            await publisher.emit(JOB_UPDATED, self.view(job))
        except BaseException:
            logger.exception("job update publish failed for %s", job.id)

    async def _notify_complete(self, job: Job) -> None:
        publisher = self._publishers.get(self._scopes.get(job.id), self._publisher)
        if publisher is None or self._closing or self._scopes.get(job.id) in self._closing_scopes:
            return
        try:
            await publisher.emit(JOB_COMPLETED, self.view(job))
        except BaseException:
            logger.exception("job completion publish failed for %s", job.id)

    def view(self, job: Job, *, scope=_ALL_SCOPES) -> JobView:
        self._require(job.id, scope=scope)
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

    def _require(self, job_id: JobId, *, scope=_ALL_SCOPES) -> Job:
        job = self.get_or_none(job_id, scope=scope)
        if job is None:
            raise JobNotFound(job_id)
        return job


def _result_bytes(result) -> int:
    if result is None:
        return 0
    measure = getattr(result, "retained_bytes", None)
    if not callable(measure):
        raise TypeError("job result must provide retained_bytes()")
    size = measure()
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("job result retained_bytes() must return a non-negative integer")
    return size


def _completed_bytes(job: Job) -> int:
    # Fixed allowance covers the registry's per-entry maps and LRU bookkeeping.
    identity, state = job.identity, job.state
    size = 1024 + sum(_value_bytes(value) for value in (
        job, identity, job.spec, state, identity.id, identity.owner,
        identity.parent, identity.name, identity.created_at, job.spec.kind,
        job.spec.label, job.started_at, job.finished_at, state.kind,
    ))
    if job.error is not None:
        error = job.error
        size += sum(_value_bytes(value) for value in (
            error, error.code, error.message, error.detail,
        ))
    if isinstance(state, (CancelledBeforeStart, CancelledRunning)):
        size += _value_bytes(state.reason)
    return size


def _value_bytes(value) -> int:
    if isinstance(value, str):
        return sys.getsizeof("") + 4 * len(value)
    return sys.getsizeof(value)


def normalize_error(exc: BaseException) -> JobError:
    code = getattr(exc, "job_error_code", None) or getattr(exc, "code", None) or "job_failed"
    return JobError(code=str(code), message=str(exc) or type(exc).__name__)


def _preview(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[:limit]}\n[truncated]"


__all__ = ["JobRegistry", "normalize_error"]


class ThreadJobs:
    """Bind thread authority; all state and operations belong to the registry."""
    def __init__(self, registry: JobRegistry, key, inbox=None):
        self._registry = registry
        self._key = key
        self._inbox = inbox

    @property
    def closing(self):
        return self._registry.closing or self._registry._bindings.get(self._key.key) is not self._key or self._key in self._registry._closing_scopes

    async def create(self, *, spec, owner, parent_job_id=None, name=None):
        if self.closing:
            raise JobRegistryClosed("thread_closing")
        if parent_job_id is not None:
            self._registry._require(parent_job_id, scope=self._key)
        return await self._registry.create(spec=spec, owner=owner, parent_job_id=parent_job_id, name=name, scope=self._key)

    def start(self, job_id, runner):
        return self._registry.start(job_id, runner, scope=self._key)
    def get_or_none(self, job_id):
        return self._registry.get_or_none(job_id, scope=self._key)
    def all(self):
        return self._registry.all(scope=self._key)
    def view(self, job):
        return self._registry.view(job, scope=self._key)
    def views(self):
        return self._registry.views(scope=self._key)
    def is_busy(self):
        return self._registry.is_busy(scope=self._key)
    def list(self, **kwargs):
        return self._registry.list(scope=self._key, **kwargs)
    async def wait(self, ids=None, **kwargs):
        return await self._registry.wait(ids, scope=self._key, inbox=self._inbox, **kwargs)
    async def cancel(self, job_id):
        return await self._registry.cancel(job_id, scope=self._key)
    async def stop_all(self):
        return await self._registry.stop_all(scope=self._key)
    async def shutdown(self):
        return await self._registry.shutdown_scope(self._key)
    def remove(self, job_id):
        return self._registry.remove(job_id, scope=self._key)
    def remove_all(self):
        return self._registry.remove_all(scope=self._key)
