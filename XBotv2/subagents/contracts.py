"""Public declarations owned by the subagent capability."""

from pydantic import BaseModel, ConfigDict, Field


class SubagentsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float = Field(default=600.0, gt=0)


class SubagentAgentError(RuntimeError):
    code = "agent_not_found"


__all__ = ["SubagentAgentError", "SubagentsConfig"]
