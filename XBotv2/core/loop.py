"""Data carried by the core agent loop.

This module contains state and settings only. Persistence, context assembly,
providers, interactions, and other runtime capabilities remain plugin-owned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from XBotv2.core.messages import Message
from XBotv2.core.runtime import SessionInfo

DEFAULT_MAX_ITERATIONS = 200


@dataclass(slots=True)
class LoopState:
    """Mutable conversation state consumed by the loop."""

    session: SessionInfo
    messages: list[Message] = field(default_factory=list)
    turn_count: int = 0
    resumed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    inbox_events: list[dict[str, Any]] = field(default_factory=list)
    media_root: str = ""

    def replace_messages(self, messages: list[Message]) -> None:
        """Replace history while preserving the state projection invariant."""
        self.messages = list(messages)
        self.turn_count = sum(message.role == "user" for message in self.messages)
        self.session.turn_count = self.turn_count


@dataclass(frozen=True, slots=True)
class LoopSettings:
    """Provider-neutral values needed to construct model requests."""

    provider: str
    model: str = ""
    model_mode: str = ""
    context_window: int = 0
    max_output_tokens: int = 0
    agent_name: str = "XBotv2"
    agent_role: str = ""
    user_name: str = "User"
    user_id: str = "default-user"
    developer_instructions: str = ""
    agent_instructions: str = ""
    memory: str = ""
    workspace: str = "."
    llm_is_override: bool = False


@dataclass(frozen=True, slots=True)
class LoopFactoryOptions:
    """Resolved core ports consumed by a concrete loop factory."""

    model_client: Any
    tools: Any
    events: Any
    state: LoopState
    settings: LoopSettings
    max_iterations: int
