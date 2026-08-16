"""Unified runtime job subsystem.

Internal jobs (subagent, shell, and future kinds) share one lifecycle model so
the registry owns waiting, cancellation, output storage, and cleanup. Domain
adapters implement a ``JobRunner`` and expose typed, model-facing tools; the
generic ``job``/``task`` vocabulary never reaches the model.
"""

from XBotv2.jobs.model import (
    CancelResult,
    Job,
    JobError,
    JobId,
    JobKind,
    JobNotFound,
    JobRegistryClosed,
    JobResult,
    JobStatus,
    JobSummary,
    MAX_SUMMARY_CHARS,
    TERMINAL_STATES,
    WaitResult,
)
from XBotv2.jobs.output import (
    CombinedShellOutput,
    OutputChunk,
    OutputStore,
    StreamOutputStore,
    TextOutputStore,
)
from XBotv2.jobs.registry import JobRegistry, job_summary, normalize_error
from XBotv2.jobs.runner import JobContext, JobRunner

__all__ = [
    "CancelResult",
    "CombinedShellOutput",
    "Job",
    "JobContext",
    "JobError",
    "JobId",
    "JobKind",
    "JobNotFound",
    "JobRegistry",
    "JobRegistryClosed",
    "JobResult",
    "JobRunner",
    "JobStatus",
    "JobSummary",
    "MAX_SUMMARY_CHARS",
    "OutputChunk",
    "OutputStore",
    "StreamOutputStore",
    "TERMINAL_STATES",
    "TextOutputStore",
    "WaitResult",
    "job_summary",
    "normalize_error",
]
