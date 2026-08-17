"""Unified runtime job subsystem.

Internal jobs (subagent, shell, and future kinds) share one lifecycle model so
the registry owns waiting, cancellation, output storage, and cleanup. Domain
adapters implement a ``JobRunner`` and expose typed, model-facing tools; the
generic ``job``/``task`` vocabulary never reaches the model.
"""

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
    "CombinedShellOutput",
    "JobContext",
    "JobRegistry",
    "JobRunner",
    "OutputChunk",
    "OutputStore",
    "StreamOutputStore",
    "TextOutputStore",
    "job_summary",
    "normalize_error",
]
