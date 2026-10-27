"""Typed notifications owned by the active Agent capability."""

from __future__ import annotations

from dataclasses import dataclass
from xcore.api.events import EventKey
from XBotv2.core.api.domain import ResolvedRuntimeSelection
from XBotv2.session.contracts import SessionKey

@dataclass(frozen=True, slots=True)
class AgentConfigured:
    runtime_selection: ResolvedRuntimeSelection
    session_key: SessionKey


AGENT_CONFIGURED = EventKey[AgentConfigured]("agent/configured")

__all__ = ["AGENT_CONFIGURED", "AgentConfigured"]
