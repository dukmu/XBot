"""Strict persisted Goal state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from pydantic import Field, field_validator

from XBotv2.core.state import JsonStateModel

GOAL_SCHEMA_VERSION = 1
GoalStatus = Literal["active", "complete", "blocked", "paused"]
GOAL_STATUSES = frozenset({"active", "complete", "blocked", "paused"})


class GoalSnapshot(JsonStateModel):
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

    @field_validator("status", mode="before")
    @classmethod
    def _validate_status(cls, value: object) -> object:
        if value not in GOAL_STATUSES:
            raise ValueError(f"Unsupported Goal status: {value!r}")
        return value


__all__ = ["GOAL_SCHEMA_VERSION", "GOAL_STATUSES", "GoalSnapshot", "GoalStatus"]
