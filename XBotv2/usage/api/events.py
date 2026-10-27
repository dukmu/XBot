"""Usage accounting events."""
from typing import Literal
from pydantic import BaseModel, ConfigDict
from XBotv2.core.domain import UsageSnapshot

class UsageUpdated(BaseModel):
    kind: Literal["usage_updated"] = "usage_updated"
    snapshot: UsageSnapshot
    model_config = ConfigDict(extra="forbid", frozen=True)
