"""Strict persisted Goal state.

One session-scoped completion condition plus everything the status view needs:
the latest evaluator verdict, how long it has been running, how many turns the
evaluator has judged, and the consumption recorded while it was active.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

GOAL_SCHEMA_VERSION = 3
#: ``active`` runs the loop. ``achieved``/``failed`` are the two terminal
#: verdicts. ``paused`` stopped automatic retries. ``cleared`` is a manual or
#: unrecoverable-error stop. Every status except ``active`` keeps the record so
#: ``/goal`` can still report it.
GoalStatus = Literal["active", "achieved", "failed", "paused", "cleared"]
GOAL_STATUSES = frozenset({"active", "achieved", "failed", "paused", "cleared"})
GoalVerdictValue = Literal["not_yet_met", "met", "impossible"]
GOAL_VERDICT_VALUES = frozenset({"not_yet_met", "met", "impossible"})

MAX_GOAL_CONDITION_CHARS = 4_000


class GoalConfig(BaseModel):
    """Loop timings, overridable per mount (tests use short intervals)."""

    model_config = ConfigDict(extra="forbid")

    #: First check-in delay once background work keeps a goal waiting.
    checkin_seconds: float = Field(default=1_800.0, gt=0)
    #: Check-in backoff cap as a multiple of ``checkin_seconds``.
    checkin_max_factor: float = Field(default=4.0, ge=1.0)
    #: Automatic retries after a recoverable turn error before pausing.
    max_retries: int = Field(default=3, ge=0)
    #: Base delay before a retry; doubles with each consumed retry.
    retry_seconds: float = Field(default=30.0, gt=0)
    #: Consecutive tool-less turns that stop the loop.
    stall_turns: int = Field(default=3, ge=1)
    #: Idle check-ins allowed between human prompts.
    max_idle_checkins: int = Field(default=3, ge=0)
    #: Hard cap on admitted goal rounds, mirroring DSH ``maxGoalRounds``.
    max_rounds: int = Field(default=20, ge=1)


class GoalStats(BaseModel):
    """Consumption observed while the goal was active."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    todo_items: int = Field(default=0, ge=0)
    todo_completed: int = Field(default=0, ge=0)


class GoalVerdict(BaseModel):
    """One evaluator answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: GoalVerdictValue
    reason: str = ""


class GoalSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    condition: str = Field(min_length=1)
    status: GoalStatus = "active"
    #: Latest evaluator reason, shown by the status view.
    reason: str = ""
    turns_evaluated: int = Field(default=0, ge=0)
    #: Epoch seconds; duration is ``finished_at - started_at`` while terminated.
    started_at: float = 0.0
    finished_at: float = 0.0
    #: Automatic retries consumed after recoverable turn errors.
    retries: int = Field(default=0, ge=0)
    #: Consecutive evaluated turns that used no tool at all.
    tool_less_turns: int = Field(default=0, ge=0)
    #: Idle check-ins delivered since the last human prompt.
    idle_checkins: int = Field(default=0, ge=0)
    #: Current check-in backoff interval in seconds; 0 uses the configured base.
    checkin_seconds: float = Field(default=0.0, ge=0.0)
    #: The loop stopped automatically after repeated turns without tool use.
    stalled: bool = False
    stats: GoalStats = Field(default_factory=GoalStats)
    schema_version: Literal[3] = GOAL_SCHEMA_VERSION

    @field_validator("condition")
    @classmethod
    def _strip_condition(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Goal condition must be a non-empty string")
        return value

    def duration_seconds(self, *, now: float) -> float:
        end = self.finished_at or now
        return max(0.0, end - self.started_at) if self.started_at else 0.0


__all__ = [
    "GOAL_SCHEMA_VERSION",
    "GoalConfig",
    "GOAL_STATUSES",
    "GOAL_VERDICT_VALUES",
    "GoalSnapshot",
    "GoalStats",
    "GoalStatus",
    "GoalVerdict",
    "GoalVerdictValue",
    "MAX_GOAL_CONDITION_CHARS",
]
