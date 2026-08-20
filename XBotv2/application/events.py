"""Typed notifications owned by application composition."""

from __future__ import annotations

from dataclasses import dataclass

from XBotv2.agentloop import LoopSettings
from XBotv2.agents import AgentDefinition
from XBotv2.core import ClientEvent
from XBotv2.session import SessionInfo


APPLICATION_INITIALIZED = "session/init"
RUNTIME_EVENT = "runtime/event"


@dataclass(frozen=True, slots=True)
class ApplicationInitialized:
    agent: AgentDefinition | None
    session: SessionInfo
    settings: LoopSettings


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    client_event: ClientEvent


__all__ = [
    "APPLICATION_INITIALIZED",
    "ApplicationInitialized",
    "RUNTIME_EVENT",
    "RuntimeEvent",
]
