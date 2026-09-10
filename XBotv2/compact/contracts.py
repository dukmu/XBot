"""Public configuration and protocol contracts for conversation compaction."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CompactConfig(BaseModel):
    """The persisted and resolved configuration for ``compact``."""

    model_config = ConfigDict(extra="forbid")

    automatic: bool = True
    output_reservation: int | None = Field(default=None, ge=0)
    trigger_ratio: float = Field(default=0.8, gt=0.0, le=1.0)
    keep_recent_turns: int = Field(default=4, ge=1)
    summary_max_chars: int = Field(default=8_000, ge=1)


__all__ = ["CompactConfig"]
