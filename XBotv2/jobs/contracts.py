"""Typed operations owned by the background-jobs capability."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from XBotv2.core.operations import EmptyRequest, Operation


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
    "LIST_TASKS",
    "STOP_ALL_TASKS",
    "STOP_TASK",
    "StopTask",
    "StoppedTasks",
    "TaskCatalog",
    "TaskSnapshot",
    "task_snapshot",
]
