"""Public declarations for the background-jobs plugin."""

from XBotv2.jobs.commands import JobsCommandPort, build_jobs_commands
from XBotv2.jobs.contracts import (
    TERMINAL_STATES,
    CancelResult,
    Job,
    JobError,
    JobId,
    JobKind,
    JobNotFound,
    JobOutputFactoryPort,
    JobRegistryClosed,
    JobResult,
    JobRunner,
    JobRunnerContext,
    JobsPort,
    JobStatus,
    JobSummary,
    LIST_TASKS,
    MAX_SUMMARY_CHARS,
    OutputChunk,
    OutputStore,
    STOP_ALL_TASKS,
    STOP_TASK,
    StopTask,
    StoppedTasks,
    TaskCatalog,
    TaskSnapshot,
    TextOutputStorePort,
    WaitMode,
    WaitResult,
    task_snapshot,
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
    "JobsCommandPort",
    "LIST_TASKS",
    "MAX_SUMMARY_CHARS",
    "OutputChunk",
    "OutputStore",
    "STOP_ALL_TASKS",
    "STOP_TASK",
    "StopTask",
    "StoppedTasks",
    "TaskCatalog",
    "TaskCompletionData",
    "TaskListResponse",
    "TaskSnapshot",
    "TaskStopResponse",
    "TaskUpdatedData",
    "TERMINAL_STATES",
    "TextOutputStorePort",
    "WaitMode",
    "WaitResult",
    "build_jobs_commands",
    "task_completion_event",
    "task_snapshot",
    "task_updated_event",
]

_PROTOCOL_EXPORTS = {
    "TaskCompletionData",
    "TaskListResponse",
    "TaskStopResponse",
    "TaskUpdatedData",
    "task_completion_event",
    "task_updated_event",
}


def __getattr__(name: str) -> object:
    if name not in _PROTOCOL_EXPORTS:
        raise AttributeError(name)
    from XBotv2.jobs import protocol

    return getattr(protocol, name)
