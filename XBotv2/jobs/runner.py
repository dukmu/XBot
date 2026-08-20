"""Internal JobRunner context and output factory.

Public runner contracts live in ``jobs.contracts``; this module implements the
context created by JobRegistry for one execution.
"""

from __future__ import annotations

from XBotv2.jobs.contracts import OutputStore
from XBotv2.jobs.output import StreamOutputStore, TextOutputStore


class JobContext:
    """Capabilities a runner uses while executing one job."""

    def __init__(self) -> None:
        self.outputs = _OutputFactory()
        self.primary_output: OutputStore | None = None


class _OutputFactory:
    """Creates job-owned output stores for runners."""

    @staticmethod
    def create_text(text: str = "") -> TextOutputStore:
        return TextOutputStore(text)

    @staticmethod
    def create_stream() -> StreamOutputStore:
        return StreamOutputStore()


__all__ = ["JobContext"]
