"""Shared lifecycle contracts for owner-defined background jobs."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

from XBotv2.core.operations import EmptyRequest, Operation
from pydantic import BaseModel, ConfigDict, Field


class JobsConfig(BaseModel):
    max_concurrent_subagents: int = Field(default=4, ge=1)
    model_config = ConfigDict(extra="forbid")

JobId = str
JobStateName = Literal[
    "queued", "running", "succeeded", "failed_before_start", "failed_running",
    "cancelled_before_start", "cancelled_running",
]
WaitMode = Literal["any", "all"]
MAX_SUMMARY_CHARS = 256


class JobSpec(Protocol):
    kind: str
    label: str


@dataclass(frozen=True, slots=True)
class OutputPage:
    data: str
    next_cursor: int | None = None
    eof: bool = False
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class JobError:
    code: str
    message: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class Queued:
    kind: Literal["queued"] = "queued"


@dataclass(frozen=True, slots=True)
class Running:
    started_at: float
    kind: Literal["running"] = "running"


@dataclass(frozen=True, slots=True)
class Succeeded:
    started_at: float
    finished_at: float
    result: object
    kind: Literal["succeeded"] = "succeeded"


@dataclass(frozen=True, slots=True)
class FailedBeforeStart:
    finished_at: float
    error: JobError
    kind: Literal["failed_before_start"] = "failed_before_start"


@dataclass(frozen=True, slots=True)
class FailedRunning:
    started_at: float
    finished_at: float
    error: JobError
    kind: Literal["failed_running"] = "failed_running"


@dataclass(frozen=True, slots=True)
class CancelledBeforeStart:
    finished_at: float
    reason: str
    kind: Literal["cancelled_before_start"] = "cancelled_before_start"


@dataclass(frozen=True, slots=True)
class CancelledRunning:
    started_at: float
    finished_at: float
    reason: str
    kind: Literal["cancelled_running"] = "cancelled_running"


JobState = (
    Queued | Running | Succeeded | FailedBeforeStart | FailedRunning
    | CancelledBeforeStart | CancelledRunning
)


@dataclass(frozen=True, slots=True)
class JobIdentity:
    id: JobId
    owner: str
    parent: JobId | None
    name: str | None
    created_at: float


@dataclass(slots=True)
class Job:
    identity: JobIdentity
    spec: JobSpec
    state: JobState = field(default_factory=Queued)

    @property
    def id(self) -> JobId: return self.identity.id
    @property
    def kind(self) -> str: return self.spec.kind
    @property
    def parent_job_id(self) -> JobId | None: return self.identity.parent
    @property
    def name(self) -> str | None: return self.identity.name
    @property
    def created_at(self) -> float: return self.identity.created_at
    @property
    def terminal(self) -> bool: return isinstance(self.state, (Succeeded, FailedBeforeStart, FailedRunning, CancelledBeforeStart, CancelledRunning))
    @property
    def status(self) -> JobStateName: return self.state.kind
    @property
    def started_at(self) -> float | None:
        if isinstance(self.state, (Running, Succeeded, FailedRunning, CancelledRunning)):
            return self.state.started_at
        return None
    @property
    def finished_at(self) -> float | None:
        if isinstance(self.state, (Succeeded, FailedBeforeStart, FailedRunning, CancelledBeforeStart, CancelledRunning)):
            return self.state.finished_at
        return None
    @property
    def result(self) -> object | None: return self.state.result if isinstance(self.state, Succeeded) else None
    @property
    def error(self) -> JobError | None:
        return self.state.error if isinstance(self.state, (FailedBeforeStart, FailedRunning)) else None
    @property
    def elapsed_ms(self) -> int:
        start = self.started_at or self.created_at
        end = self.finished_at or time.time()
        return max(0, int((end - start) * 1000))


class JobView(BaseModel):
    id: JobId = Field(min_length=1)
    kind: str = Field(min_length=1)
    label: str = Field(min_length=1)
    state: JobStateName
    elapsed_ms: int = Field(ge=0)
    summary: str | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class WaitResult:
    ready: tuple[JobView, ...]
    pending: tuple[JobId, ...]
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class CancelResult:
    id: JobId
    status: JobStateName
    cancelled: bool = False


class JobNotFound(KeyError): pass
class JobRegistryClosed(RuntimeError): pass


class JobRunner(Protocol):
    async def run(self, job: Job) -> object: ...
    async def cancel(self, job: Job) -> None: ...


class JobsPort(Protocol):
    @property
    def closing(self) -> bool: ...
    async def create(self, *, spec: JobSpec, owner: str, parent_job_id: JobId | None = None, name: str | None = None) -> Job: ...
    def start(self, job_id: JobId, runner: JobRunner) -> Job: ...
    def get_or_none(self, job_id: JobId) -> Job | None: ...
    def all(self) -> list[Job]: ...
    def list(self, *, kind: str | None = None, status: JobStateName | None = None, parent_job_id: JobId | None = None, recursive: bool = False, max_results: int = 20) -> list[JobView]: ...
    async def wait(self, ids: list[JobId], *, mode: WaitMode = "all", timeout: float | None = None) -> WaitResult: ...
    async def cancel(self, job_id: JobId) -> CancelResult: ...


class JobsCommandPort(Protocol):
    def views(self) -> list[JobView]: ...
    def get_or_none(self, job_id: str) -> Job | None: ...
    async def cancel(self, job_id: str) -> CancelResult: ...
    async def stop_all(self) -> list[JobView]: ...


@dataclass(frozen=True, slots=True)
class JobCatalog:
    jobs: tuple[JobView, ...]


@dataclass(frozen=True, slots=True)
class StopJob:
    job_id: str


@dataclass(frozen=True, slots=True)
class StoppedJobs:
    jobs: tuple[JobView, ...]


LIST_JOBS = Operation("jobs/list", EmptyRequest, JobCatalog)
STOP_JOB = Operation("jobs/stop", StopJob, StoppedJobs)
STOP_ALL_JOBS = Operation("jobs/stop-all", EmptyRequest, StoppedJobs)
JOB_UPDATED = "job/updated"
JOB_COMPLETED = "job/completed"


class JobEventPort(Protocol):
    async def emit(self, event: str, *args: object) -> None: ...


def parse_job_status(value: str | None) -> JobStateName | None:
    if value is None: return None
    allowed = {"queued", "running", "succeeded", "failed_before_start", "failed_running", "cancelled_before_start", "cancelled_running"}
    return value if value in allowed else None  # type: ignore[return-value]


__all__ = [
    "CancelResult", "CancelledBeforeStart", "CancelledRunning", "FailedBeforeStart", "FailedRunning",
    "Job", "JobCatalog", "JobError", "JobEventPort", "JobId", "JobIdentity", "JobNotFound",
    "JobRegistryClosed", "JobRunner",
    "JobSpec", "JobState", "JobStateName", "JobView", "JobsCommandPort", "JobsPort",
    "JOB_COMPLETED", "JOB_UPDATED", "LIST_JOBS", "MAX_SUMMARY_CHARS", "OutputPage", "Queued",
    "Running", "STOP_ALL_JOBS", "STOP_JOB", "StopJob", "StoppedJobs", "Succeeded",
    "WaitMode", "WaitResult", "parse_job_status",
]
