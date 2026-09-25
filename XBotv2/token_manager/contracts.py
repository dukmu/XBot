"""Typed values produced by the token manager's request estimator."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class EstimatedContext(BaseModel):
    """One explicit estimate; it is never a provider observation."""

    tokens: int = Field(ge=0)
    method: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


__all__ = ["EstimatedContext"]
