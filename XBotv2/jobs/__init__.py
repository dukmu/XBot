"""Public declarations for the background-jobs plugin."""

from XBotv2.jobs.commands import JobsCommandPort, build_jobs_commands
from XBotv2.jobs.contracts import (
    LIST_TASKS,
    STOP_ALL_TASKS,
    STOP_TASK,
    StopTask,
    StoppedTasks,
    TaskCatalog,
    TaskSnapshot,
    task_snapshot,
)

__all__ = [
    "JobsCommandPort",
    "LIST_TASKS",
    "STOP_ALL_TASKS",
    "STOP_TASK",
    "StopTask",
    "StoppedTasks",
    "TaskCatalog",
    "TaskListResponse",
    "TaskSnapshot",
    "TaskStopResponse",
    "TaskUpdatedData",
    "build_jobs_commands",
    "task_snapshot",
]

_PROTOCOL_EXPORTS = {
    "TaskListResponse",
    "TaskStopResponse",
    "TaskUpdatedData",
}


def __getattr__(name: str) -> object:
    if name not in _PROTOCOL_EXPORTS:
        raise AttributeError(name)
    from XBotv2.jobs import protocol

    return getattr(protocol, name)
