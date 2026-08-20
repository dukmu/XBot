"""Public domain, service, runner, and operation contracts for Jobs."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Protocol

from XBotv2.core.operations import EmptyRequest, Operation
from XBotv2.core.tools import JsonObject

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


@dataclass(slots=True)
class JobError:
    code: str
    message: str
    detail: str | None = None

    def to_dict(self) -> dict[str, str]:
        value: dict[str, str] = {"code": self.code, "message": self.message}
        if self.detail:
            value["detail"] = self.detail
        return value


@dataclass(slots=True)
class JobResult:
    summary: str | None = None
    output_store: OutputStore | None = None
    data: JsonObject = field(default_factory=dict)


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
    metadata: JsonObject = field(default_factory=dict)
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


@dataclass(frozen=True, slots=True)
class JobSummary:
    id: JobId
    kind: str
    status: str
    name: str | None = None
    created_at: float = 0.0
    elapsed_ms: int = 0
    parent_job_id: JobId | None = None
    summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.name:
            value["name"] = self.name
        if self.parent_job_id:
            value["parent_job_id"] = self.parent_job_id
        if self.summary:
            value["summary"] = self.summary
        return value


@dataclass(frozen=True, slots=True)
class WaitResult:
    ready: list[JobSummary] = field(default_factory=list)
    pending: list[JobId] = field(default_factory=list)
    timed_out: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": [summary.to_dict() for summary in self.ready],
            "pending": list(self.pending),
            "timed_out": self.timed_out,
        }


@dataclass(frozen=True, slots=True)
class CancelResult:
    id: JobId
    status: str
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "cancelled": self.cancelled,
        }


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
        metadata: JsonObject | None = None,
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


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    task_id: str
    kind: Literal["shell", "agent"]
    command: str
    cwd: str
    status: Literal["pending", "running", "completed", "failed", "stopped"]
    created_at: float
    started_at: float
    finished_at: float
    output: str = ""
    error: str = ""
    agent: str = ""
    thread_id: str = ""
    usage: dict[str, object] = field(default_factory=dict)


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


def task_snapshot(data: dict[str, object]) -> TaskSnapshot:
    """Validate the registry's internal snapshot at the capability boundary."""
    kind = str(data["kind"])
    status = str(data["status"])
    if kind not in {"shell", "agent"}:
        raise ValueError(f"invalid task kind: {kind}")
    if status not in {"pending", "running", "completed", "failed", "stopped"}:
        raise ValueError(f"invalid task status: {status}")
    return TaskSnapshot(
        task_id=str(data["task_id"]),
        kind=kind,  # type: ignore[arg-type]
        command=str(data["command"]),
        cwd=str(data["cwd"]),
        status=status,  # type: ignore[arg-type]
        created_at=float(data["created_at"]),
        started_at=float(data["started_at"]),
        finished_at=float(data["finished_at"]),
        output=str(data.get("output") or ""),
        error=str(data.get("error") or ""),
        agent=str(data.get("agent") or ""),
        thread_id=str(data.get("thread_id") or ""),
        usage=dict(data.get("usage") or {}),  # type: ignore[arg-type]
    )


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
    "task_snapshot",
]
