"""Strict persisted Goal state."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

GOAL_SCHEMA_VERSION = 1
GoalStatus = Literal["active", "complete", "blocked", "paused"]
GOAL_STATUSES = frozenset({"active", "complete", "blocked", "paused"})


class GoalSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: str = Field(min_length=1)
    status: GoalStatus = "active"
    summary: str = ""
    token_budget: int | None = Field(default=None, gt=0)
    schema_version: Literal[1] = GOAL_SCHEMA_VERSION

    @field_validator("objective")
    @classmethod
    def _strip_objective(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Goal objective must be a non-empty string")
        return value

__all__ = ["GOAL_SCHEMA_VERSION", "GOAL_STATUSES", "GoalSnapshot", "GoalStatus"]
