"""Strict persisted Goal state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from XBotv2.core.tools import JsonObject

GOAL_SCHEMA_VERSION = 1
GoalStatus = Literal["active", "complete", "blocked", "paused"]
GOAL_STATUSES = frozenset({"active", "complete", "blocked", "paused"})


@dataclass(frozen=True, slots=True)
class GoalSnapshot:
    objective: str
    status: GoalStatus = "active"
    summary: str = ""
    token_budget: int | None = None
    schema_version: int = GOAL_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "GoalSnapshot":
        expected = {
            "schema_version", "objective", "status", "summary", "token_budget",
        }
        if set(value) != expected:
            raise ValueError("GoalSnapshot contains unknown or missing fields")
        version = value["schema_version"]
        objective = value["objective"]
        status = value["status"]
        summary = value["summary"]
        budget = value["token_budget"]
        if type(version) is not int or version != GOAL_SCHEMA_VERSION:
            raise ValueError(f"Unsupported GoalSnapshot schema version: {version!r}")
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("Goal objective must be a non-empty string")
        if status not in GOAL_STATUSES:
            raise ValueError(f"Unsupported Goal status: {status!r}")
        if not isinstance(summary, str):
            raise TypeError("Goal summary must be a string")
        if budget is not None and (
            isinstance(budget, bool) or not isinstance(budget, int) or budget < 1
        ):
            raise ValueError("Goal token_budget must be null or a positive integer")
        return cls(
            objective=objective.strip(),
            status=cast(GoalStatus, status),
            summary=summary,
            token_budget=budget,
        )

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "objective": self.objective,
            "status": self.status,
            "summary": self.summary,
            "token_budget": self.token_budget,
        }


__all__ = ["GOAL_SCHEMA_VERSION", "GOAL_STATUSES", "GoalSnapshot", "GoalStatus"]
