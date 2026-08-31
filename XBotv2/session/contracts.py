"""Public contracts for session management and Agent application creation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from XBotv2.application import AgentApplicationPort
from XBotv2.agents import AgentDefinition
from XBotv2.core.messages import Message
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.providers import BaseProvider
from XBotv2.core.tools import JsonObject
from XBotv2.permissions import PermissionsPort
from XBotv2.session.types import SessionSnapshot


PREPARE_FORK = "session/prepare-fork"
HISTORY_CHANGED = "session/history-changed"
SESSION_RESOURCE_CHANGED = "session/resource-changed"
SESSION_RESOURCE_REMOVED = "session/resource-removed"


@dataclass(frozen=True, slots=True)
class PrepareFork:
    session_id: str
    thread_id: str


@dataclass(frozen=True, slots=True)
class HistoryChanged:
    messages: tuple[Message, ...]
    operation: str
    turns: int = 0


@dataclass(frozen=True, slots=True)
class SessionResourceChanged:
    session: SessionSnapshot
    added: bool = False


@dataclass(frozen=True, slots=True)
class SessionResourceRemoved:
    session_id: str


@dataclass(frozen=True, slots=True)
class SessionStatus:
    session_id: str
    thread_id: str
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class AgentApplicationOptions:
    """Launch facts for one session-owned Agent application."""

    paths: RuntimePaths
    provider_name: str
    session_id: str
    thread_id: str
    workspace_root: Path
    no_plugins: bool
    plugin_configs: dict[str, JsonObject] | None = None
    model_override: BaseProvider | None = None
    selected_agent: str | None = None
    agent_definition: AgentDefinition | None = None
    parent_thread_id: str = ""
    parent_permission_system: PermissionsPort | None = None
    is_subagent: bool = False
    interactive: bool = True


class AgentApplicationFactory(Protocol):
    """Composition-owned factory consumed by process session management."""

    async def __call__(self, options: AgentApplicationOptions) -> AgentApplicationPort: ...


__all__ = [
    "AgentApplicationFactory",
    "AgentApplicationOptions",
    "HISTORY_CHANGED",
    "HistoryChanged",
    "PREPARE_FORK",
    "PrepareFork",
    "SESSION_RESOURCE_CHANGED",
    "SESSION_RESOURCE_REMOVED",
    "SessionResourceChanged",
    "SessionResourceRemoved",
    "SessionStatus",
]
