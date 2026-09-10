"""Internal JobRunner context and output factory.

Public runner contracts live in ``jobs.contracts``; this module implements the
context created by JobRegistry for one execution.
"""

from __future__ import annotations

from XBotv2.jobs.contracts import TextOutputStorePort
from XBotv2.jobs.output import TextOutputStore


class JobContext:
    """Capabilities a runner uses while executing one job."""

    def __init__(self) -> None:
        self.outputs = _OutputFactory()
        self.primary_output: TextOutputStorePort | None = None


class _OutputFactory:
    """Creates job-owned output stores for runners."""

    @staticmethod
    def create_text(text: str = "") -> TextOutputStore:
        return TextOutputStore(text)


__all__ = ["JobContext"]
