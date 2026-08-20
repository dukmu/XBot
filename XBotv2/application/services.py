"""Public composition services for launching child Agent applications."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from XBotv2.core.agents import AgentDefinition, AgentSession
from XBotv2.core.paths import SessionPaths


@dataclass(frozen=True, slots=True)
class SessionLaunch:
    session_id: str
    thread_id: str
    workspace_root: Path
    provider_name: str
    session_paths: SessionPaths
    interactive: bool
    is_subagent: bool


@dataclass(frozen=True, slots=True)
class ParentPermissions:
    value: object | None


@dataclass(frozen=True, slots=True)
class ChildApplicationRequest:
    definition: AgentDefinition
    thread_id: str
    prompt: str
    parent_permissions: object
    client_events: object | None


class ChildApplicationsPort(Protocol):
    async def spawn(self, request: ChildApplicationRequest) -> AgentSession: ...


__all__ = [
    "ChildApplicationRequest",
    "ChildApplicationsPort",
    "ParentPermissions",
    "SessionLaunch",
]
