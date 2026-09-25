"""Canonical Goal state and evaluator verdicts."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_GOAL_CONDITION_CHARS = 4_000


class GoalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkin_seconds: float = Field(default=1_800.0, gt=0)
    checkin_max_factor: float = Field(default=4.0, ge=1.0)
    max_retries: int = Field(default=3, ge=0)
    retry_seconds: float = Field(default=30.0, gt=0)
    stall_turns: int = Field(default=3, ge=1)
    max_idle_checkins: int = Field(default=3, ge=0)
    max_rounds: int = Field(default=20, ge=1)


class GoalProgress(BaseModel):
    turns_evaluated: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    tool_less_turns: int = Field(default=0, ge=0)
    idle_checkins: int = Field(default=0, ge=0)
    stalled: bool = False
    model_config = ConfigDict(extra="forbid", frozen=True)


class GoalStats(BaseModel):
    tool_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    todo_items: int = Field(default=0, ge=0)
    todo_completed: int = Field(default=0, ge=0)
    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class GoalCheckinPolicy(BaseModel):
    backoff_factor: float = Field(default=1.0, ge=1.0)
    model_config = ConfigDict(extra="forbid", frozen=True)


class NoGoal(BaseModel):
    kind: Literal["none"] = "none"
    model_config = ConfigDict(extra="forbid", frozen=True)


class _GoalWithCondition(BaseModel):
    condition: str = Field(min_length=1, max_length=MAX_GOAL_CONDITION_CHARS)
    started_at: float
    stats: GoalStats = Field(default_factory=GoalStats)
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("condition")
    @classmethod
    def _strip_condition(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Goal condition must be a non-empty string")
        return value


class ActiveGoal(_GoalWithCondition):
    kind: Literal["active"] = "active"
    progress: GoalProgress = Field(default_factory=GoalProgress)
    checkin_policy: GoalCheckinPolicy = Field(default_factory=GoalCheckinPolicy)


class PausedGoal(_GoalWithCondition):
    kind: Literal["paused"] = "paused"
    paused_at: float
    reason: str
    progress: GoalProgress
    checkin_policy: GoalCheckinPolicy


class AchievedGoal(_GoalWithCondition):
    kind: Literal["achieved"] = "achieved"
    finished_at: float
    reason: str
    progress: GoalProgress


class FailedGoal(_GoalWithCondition):
    kind: Literal["failed"] = "failed"
    finished_at: float
    reason: str
    progress: GoalProgress


GoalState: TypeAlias = Annotated[
    NoGoal | ActiveGoal | PausedGoal | AchievedGoal | FailedGoal,
    Field(discriminator="kind"),
]


class GoalSnapshot(BaseModel):
    state: GoalState
    model_config = ConfigDict(extra="forbid", frozen=True)


class NotMet(BaseModel):
    kind: Literal["not_met"] = "not_met"
    reason: str
    model_config = ConfigDict(extra="forbid", frozen=True)


class Met(BaseModel):
    kind: Literal["met"] = "met"
    reason: str
    model_config = ConfigDict(extra="forbid", frozen=True)


class Impossible(BaseModel):
    kind: Literal["impossible"] = "impossible"
    reason: str
    model_config = ConfigDict(extra="forbid", frozen=True)


GoalVerdict: TypeAlias = Annotated[
    NotMet | Met | Impossible,
    Field(discriminator="kind"),
]


class GoalChanged(BaseModel):
    kind: Literal["goal_changed"] = "goal_changed"
    snapshot: GoalSnapshot
    model_config = ConfigDict(extra="forbid", frozen=True)


__all__ = [
    "AchievedGoal",
    "ActiveGoal",
    "FailedGoal",
    "GoalChanged",
    "GoalCheckinPolicy",
    "GoalConfig",
    "GoalProgress",
    "GoalSnapshot",
    "GoalState",
    "GoalStats",
    "GoalVerdict",
    "Impossible",
    "MAX_GOAL_CONDITION_CHARS",
    "Met",
    "NoGoal",
    "NotMet",
    "PausedGoal",
]
