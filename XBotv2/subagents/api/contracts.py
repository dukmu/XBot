"""Public declarations owned by the subagent capability."""

from pydantic import BaseModel, ConfigDict, Field
from xcore.api.services import ServiceKey


class SubagentsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float = Field(default=600.0, gt=0)
    max_live_threads: int = Field(default=4, ge=1)


class SubagentAgentError(RuntimeError):
    code = "agent_not_found"


SUBAGENTS = ServiceKey("subagents")
