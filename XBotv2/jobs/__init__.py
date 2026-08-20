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
    "TaskSnapshot",
    "build_jobs_commands",
    "task_snapshot",
]
