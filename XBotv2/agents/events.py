"""Typed notifications owned by the active Agent capability."""

from __future__ import annotations

from dataclasses import dataclass

from XBotv2.core.domain import ResolvedRuntimeSelection
from XBotv2.session.contracts import SessionKey


AGENT_CONFIGURED = "agent/configured"


@dataclass(frozen=True, slots=True)
class AgentConfigured:
    runtime_selection: ResolvedRuntimeSelection
    session_key: SessionKey


__all__ = ["AGENT_CONFIGURED", "AgentConfigured"]
