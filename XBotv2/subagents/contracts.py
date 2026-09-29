"""Public declarations owned by the subagent capability."""

from dataclasses import dataclass, field as dataclass_field

from pydantic import BaseModel, ConfigDict, Field

from XBotv2.core.domain import UsageSnapshot


class SubagentsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float = Field(default=600.0, gt=0)


class SubagentAgentError(RuntimeError):
    code = "agent_not_found"


class SubagentExecutionError(RuntimeError):
    code = "subagent_failed"


@dataclass(frozen=True, slots=True)
class SubagentResult:
    thread_id: str
    final_response: str
    usage: UsageSnapshot = dataclass_field(default_factory=UsageSnapshot)


__all__ = [
    "SubagentAgentError",
    "SubagentExecutionError",
    "SubagentResult",
    "SubagentsConfig",
]
