"""Public domain, service, runner, and operation contracts for Jobs."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Protocol

from XBotv2.core.operations import EmptyRequest, Operation
from pydantic import BaseModel, ConfigDict, Field, JsonValue

JobId = str
MAX_SUMMARY_CHARS = 256
WaitMode = Literal["any", "all"]


class JobKind(str, Enum):
    SUBAGENT = "subagent"
    SHELL = "shell"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset({
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
})


@dataclass(frozen=True, slots=True)
class OutputChunk:
    data: str
    next_cursor: int | None = None
    eof: bool = False
    truncated: bool = False


class OutputStore(Protocol):
    async def read(
        self,
        *,
        cursor: int | None = None,
        max_bytes: int = 8000,
    ) -> OutputChunk: ...


class TextOutputStorePort(OutputStore, Protocol):
    async def write(self, text: str) -> None: ...

    def all(self) -> str: ...


class JobOutputFactoryPort(Protocol):
    def create_text(self, text: str = "") -> TextOutputStorePort: ...


class JobRunnerContext(Protocol):
    outputs: JobOutputFactoryPort
    primary_output: OutputStore | None


class JobError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    detail: str | None = None

@dataclass(slots=True)
class JobResult:
    summary: str | None = None
    output_store: OutputStore | None = None
    data: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(slots=True)
class Job:
    id: JobId
    kind: JobKind
    status: JobStatus = JobStatus.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    parent_job_id: JobId | None = None
    name: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    result: JobResult | None = None
    error: JobError | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    @property
    def elapsed_ms(self) -> int:
        start = self.started_at if self.started_at is not None else self.created_at
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0, int((end - start) * 1000))


class JobRunner(Protocol):
    async def run(self, job: Job, ctx: JobRunnerContext) -> JobResult: ...

    async def cancel(self, job: Job) -> None: ...


class JobSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: JobId
    kind: str
    status: str
    name: str | None = None
    elapsed_ms: int = 0
    parent_job_id: JobId | None = None
    summary: str | None = None

class WaitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ready: list[JobSummary] = Field(default_factory=list)
    pending: list[JobId] = Field(default_factory=list)
    timed_out: bool = False


class CancelResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: JobId
    status: str
    cancelled: bool = False


class JobNotFound(KeyError):
    pass


class JobRegistryClosed(RuntimeError):
    pass


class JobsPort(Protocol):
    @property
    def closing(self) -> bool: ...

    async def create(
        self,
        *,
        kind: JobKind,
        metadata: dict[str, JsonValue] | None = None,
        parent_job_id: JobId | None = None,
        name: str | None = None,
    ) -> Job: ...

    def start(self, job_id: JobId, runner: JobRunner) -> Job: ...

    def get_or_none(self, job_id: JobId) -> Job | None: ...

    def all(self) -> list[Job]: ...

    def list(
        self,
        *,
        kind: JobKind | None = None,
        status: JobStatus | None = None,
        parent_job_id: JobId | None = None,
        recursive: bool = False,
        max_results: int = 20,
    ) -> list[JobSummary]: ...

    async def wait(
        self,
        ids: list[JobId],
        *,
        mode: WaitMode = "all",
        timeout: float | None = None,
    ) -> WaitResult: ...

    async def cancel(self, job_id: JobId) -> CancelResult: ...


class TaskSnapshot(BaseModel):
    task_id: str = Field(min_length=1)
    kind: Literal["shell", "agent"] = "shell"
    command: str = ""
    cwd: str
    status: Literal["pending", "running", "completed", "failed", "stopped"]
    created_at: float = Field(ge=0)
    started_at: float = Field(ge=0)
    finished_at: float = Field(ge=0)
    output: str = ""
    error: str = ""
    agent: str = ""
    thread_id: str = ""
    usage: dict[str, JsonValue] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class TaskCatalog:
    tasks: tuple[TaskSnapshot, ...]


@dataclass(frozen=True, slots=True)
class StopTask:
    task_id: str


@dataclass(frozen=True, slots=True)
class StoppedTasks:
    tasks: tuple[TaskSnapshot, ...]


LIST_TASKS = Operation("jobs/list", EmptyRequest, TaskCatalog)
STOP_TASK = Operation("jobs/stop", StopTask, StoppedTasks)
STOP_ALL_TASKS = Operation("jobs/stop-all", EmptyRequest, StoppedTasks)


__all__ = [
    "CancelResult",
    "Job",
    "JobError",
    "JobId",
    "JobKind",
    "JobNotFound",
    "JobOutputFactoryPort",
    "JobRegistryClosed",
    "JobResult",
    "JobRunner",
    "JobRunnerContext",
    "JobsPort",
    "JobStatus",
    "JobSummary",
    "LIST_TASKS",
    "MAX_SUMMARY_CHARS",
    "OutputChunk",
    "OutputStore",
    "STOP_ALL_TASKS",
    "STOP_TASK",
    "StopTask",
    "StoppedTasks",
    "TaskCatalog",
    "TaskSnapshot",
    "TERMINAL_STATES",
    "TextOutputStorePort",
    "WaitMode",
    "WaitResult",
]
