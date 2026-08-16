"""Unified job state model shared by every job kind.

Internal jobs (subagent, shell, and future kinds) use one state machine so
lifecycle, waiting, cancellation, and result storage stay kind-agnostic. The
model never surfaces a generic ``task``/``job`` tool to the model; domain tools
wrap these types behind typed adapters.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from api.jobs.output import OutputStore

JobId = str

MAX_SUMMARY_CHARS = 256


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


@dataclass
class JobError:
    """Structured, model-safe error attached to a failed job."""

    code: str
    message: str
    detail: str | None = None

    def to_dict(self) -> dict[str, str]:
        value: dict[str, str] = {"code": self.code, "message": self.message}
        if self.detail:
            value["detail"] = self.detail
        return value


@dataclass
class JobResult:
    """Workload-specific completion payload.

    ``summary`` is small enough for status and list responses. Bulk text must
    live in ``output_store`` and be read through an explicit ``read_*`` tool.
    """

    summary: str | None = None
    output_store: "OutputStore | None" = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Job:
    """One tracked workload in a JobRegistry."""

    id: JobId
    kind: JobKind
    status: JobStatus = JobStatus.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    parent_job_id: JobId | None = None
    name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    result: JobResult | None = None
    error: JobError | None = None
    completion_event: asyncio.Event | None = field(default=None, repr=False)
    runner: Any = field(default=None, repr=False)
    runner_task: asyncio.Task | None = field(default=None, repr=False)
    runtime_handle: Any = field(default=None, repr=False)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    @property
    def elapsed_ms(self) -> int:
        start = self.started_at if self.started_at is not None else self.created_at
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0, int((end - start) * 1000))


@dataclass(frozen=True)
class JobSummary:
    """Lightweight, model-facing view of one job.

    Never contains prompts, full commands, output, or runtime handles.
    """

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


@dataclass(frozen=True)
class WaitResult:
    """Lightweight result of waiting on one or more jobs."""

    ready: list[JobSummary] = field(default_factory=list)
    pending: list[JobId] = field(default_factory=list)
    timed_out: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": [summary.to_dict() for summary in self.ready],
            "pending": list(self.pending),
            "timed_out": self.timed_out,
        }


@dataclass(frozen=True)
class CancelResult:
    """Idempotent outcome of cancelling one job."""

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
    """Raised when a job id is unknown to a JobRegistry."""


class JobRegistryClosed(RuntimeError):
    """Raised when a job is created on a registry that is shutting down."""


__all__ = [
    "CancelResult",
    "Job",
    "JobError",
    "JobId",
    "JobKind",
    "JobNotFound",
    "JobRegistryClosed",
    "JobResult",
    "JobStatus",
    "JobSummary",
    "MAX_SUMMARY_CHARS",
    "TERMINAL_STATES",
    "WaitResult",
]
