"""Goal runtime event."""
from typing import Literal
from pydantic import BaseModel, ConfigDict
from XBotv2.goal.api.models import GoalSnapshot


class GoalChanged(BaseModel):
    kind: Literal["goal_changed"] = "goal_changed"
    snapshot: GoalSnapshot
    model_config = ConfigDict(extra="forbid", frozen=True)
