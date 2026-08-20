"""Typed operations owned by the Agent tool capability."""

from __future__ import annotations

from dataclasses import dataclass, field

from XBotv2.core.operations import EmptyRequest, Operation
from XBotv2.core.messages import Message
from XBotv2.core.tools import JsonObject, Tool
from XBotv2.session import SessionInfo

DEFAULT_MAX_ITERATIONS = 200


@dataclass(slots=True)
class LoopState:
    """Mutable conversation state owned by the Agent loop."""

    session: SessionInfo
    messages: list[Message] = field(default_factory=list)
    turn_count: int = 0
    resumed: bool = False
    metadata: JsonObject = field(default_factory=dict)
    inbox_events: list[JsonObject] = field(default_factory=list)
    media_root: str = ""

    def replace_messages(self, messages: list[Message]) -> None:
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
class ToolDescription:
    name: str
    registered_name: str
    namespace: str
    description: str
    parameters: dict[str, object]
    timeout_seconds: float | None


@dataclass(frozen=True, slots=True)
class ToolCatalog:
    tools: tuple[ToolDescription, ...]


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    """Stable inspection view for one registered Tool."""

    tool: Tool
    registered_name: str
    namespace: str = "builtin"
    model_visible: bool = True
    timeout_seconds: float | None = None
    injected: dict[str, object] | None = None


LIST_TOOLS = Operation("tools/list", EmptyRequest, ToolCatalog)


__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "LIST_TOOLS",
    "LoopSettings",
    "LoopState",
    "ToolCatalog",
    "ToolDescription",
    "ToolRegistration",
]
