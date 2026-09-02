"""Typed notifications owned by the active Agent capability."""

from __future__ import annotations

from dataclasses import dataclass

from XBotv2.agents.contracts import AgentDefinition
from XBotv2.session.types import SessionInfo


AGENT_CONFIGURED = "agent/configured"


@dataclass(frozen=True, slots=True)
class AgentConfigured:
    agent: AgentDefinition | None
    session: SessionInfo
    agent_name: str
    provider: str
    model: str
    model_mode: str
    context_window: int


__all__ = ["AGENT_CONFIGURED", "AgentConfigured"]
