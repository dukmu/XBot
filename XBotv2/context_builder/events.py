"""Typed lifecycle events owned by context construction."""

from __future__ import annotations

from dataclasses import dataclass

from XBotv2.context_builder.contracts import ContextComponent
from XBotv2.core import Message
from XBotv2.session.types import SessionInfo


BEFORE_CONTEXT_BUILD = "before/context-build"
BUILD_CONTEXT = "context/build"
CONTEXT_COMPONENTS_BUILT = "after/context-components-build"
CONTEXT_BUILT = "after/context-build"


@dataclass(slots=True)
class ContextBuildRequest:
    """Mutable provider-neutral inputs and result for one context build."""

    messages: list[Message]
    session: SessionInfo | None = None
    agent_name: str = "XBotv2"
    agent_role: str = ""
    user_name: str = "User"
    user_id: str = "default-user"
    developer_instructions: str = ""
    instructions: str = ""
    memory: str = ""
    sandbox_summary: str = ""
    runtime_paths: dict[str, str] | None = None
    system_notice: str = ""
    turn_count: int = 0
    active_subagents: int = 0
    context_messages: list[Message] | None = None


@dataclass(slots=True)
class ContextComponentsBuilt:
    """Mutable component list exposed before provider rendering."""

    components: list[ContextComponent]
    session: SessionInfo | None = None


@dataclass(frozen=True, slots=True)
class ContextBuilt:
    """Notification emitted after provider-neutral context is rendered."""

    messages: tuple[Message, ...]
    session: SessionInfo | None = None


__all__ = [
    "BEFORE_CONTEXT_BUILD",
    "BUILD_CONTEXT",
    "CONTEXT_BUILT",
    "CONTEXT_COMPONENTS_BUILT",
    "ContextBuildRequest",
    "ContextBuilt",
    "ContextComponentsBuilt",
]
