"""Canonical state for the optional same-session Goal plugin."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_GOAL_OBJECTIVE_CHARS = 4_000
GoalActivation: TypeAlias = Literal["armed", "disarmed", "none"]


class GoalStats(BaseModel):
    rounds_started: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class NoGoal(BaseModel):
    kind: Literal["none"] = "none"
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Goal(BaseModel):
    goal_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    objective: str = Field(min_length=1, max_length=MAX_GOAL_OBJECTIVE_CHARS)
    started_at: float
    stats: GoalStats = Field(default_factory=GoalStats)
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("objective")
    @classmethod
    def _strip_objective(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Goal objective must be a non-empty string")
        return value


class ActiveGoal(_Goal):
    kind: Literal["active"] = "active"
    pending_input_id: str | None = None


class PausedGoal(_Goal):
    kind: Literal["paused"] = "paused"
    paused_at: float
    reason: str = Field(min_length=1)


class BlockedGoal(_Goal):
    kind: Literal["blocked"] = "blocked"
    blocked_at: float
    reason: str = Field(min_length=1)


class CompleteGoal(_Goal):
    kind: Literal["complete"] = "complete"
    finished_at: float
    reason: str = Field(min_length=1)


GoalState: TypeAlias = Annotated[
    NoGoal | ActiveGoal | PausedGoal | BlockedGoal | CompleteGoal,
    Field(discriminator="kind"),
]


class GoalSnapshot(BaseModel):
    state: GoalState
    activation: GoalActivation = "none"
    model_config = ConfigDict(extra="forbid", frozen=True)


class GoalChanged(BaseModel):
    kind: Literal["goal_changed"] = "goal_changed"
    snapshot: GoalSnapshot
    model_config = ConfigDict(extra="forbid", frozen=True)


__all__ = [
    "ActiveGoal",
    "BlockedGoal",
    "CompleteGoal",
    "GoalActivation",
    "GoalChanged",
    "GoalSnapshot",
    "GoalState",
    "GoalStats",
    "MAX_GOAL_OBJECTIVE_CHARS",
    "NoGoal",
    "PausedGoal",
]
