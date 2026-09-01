"""Typed operations owned by the Agent tool capability."""

from __future__ import annotations

from dataclasses import dataclass

from XBotv2.core.history import ConversationHistory, HistoryCheckpoint
from XBotv2.agentloop.inbox import InboxInput, InboxSink
from XBotv2.core.operations import EmptyRequest, Operation
from XBotv2.core.messages import Message
from XBotv2.core.metadata import ThreadMetadata, ThreadMetadataState
from XBotv2.core.tools import JsonObject, Tool
from XBotv2.llm import ModelPort
from XBotv2.session import SessionInfo

DEFAULT_MAX_ITERATIONS = 200


class LoopState:
    """Mutable conversation state owned by the Agent loop."""

    def __init__(
        self,
        session: SessionInfo,
        messages: list[Message] | ConversationHistory | None = None,
        turn_count: int = 0,
        resumed: bool = False,
        metadata: ThreadMetadata | JsonObject | None = None,
        inbox_items: list[InboxInput] | None = None,
        inbox_sink: InboxSink | None = None,
    ) -> None:
        self.session = session
        self.history = (
            messages
            if isinstance(messages, ConversationHistory)
            else ConversationHistory(messages or ())
        )
        self.turn_count = turn_count
        self.resumed = resumed
        self.metadata = ThreadMetadataState(
            metadata
            if isinstance(metadata, ThreadMetadata)
            else ThreadMetadata.from_state(metadata or {})
        )
        self.inbox_items = list(inbox_items or [])
        self.inbox_sink = inbox_sink

    @property
    def messages(self) -> ConversationHistory:
        return self.history

    def set_history(self, history: ConversationHistory) -> None:
        self.history = history
        self._update_turn_count()

    def replace_messages(self, messages: list[Message]) -> None:
        self.history.replace(messages)
        self._update_turn_count()

    def replace_messages_recoverable(
        self,
        messages: list[Message],
        *,
        operation: str,
        reason: str,
    ) -> HistoryCheckpoint | None:
        checkpoint = self.history.replace_recoverable(
            messages,
            operation=operation,
            reason=reason,
        )
        self._update_turn_count()
        return checkpoint

    def restore_history(
        self,
        checkpoint_id: str,
        *,
        operation: str,
    ) -> HistoryCheckpoint:
        checkpoint = self.history.restore(checkpoint_id, operation=operation)
        self._update_turn_count()
        return checkpoint

    def _update_turn_count(self) -> None:
        self.turn_count = sum(message.role == "user" for message in self.history)
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


@dataclass(slots=True)
class ModelRequest:
    """Mutable provider request exposed to Agent-loop event listeners."""

    messages: list[Message]
    tools: list[Tool]
    llm: ModelPort


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


LIST_TOOLS = Operation("tools/list", EmptyRequest, ToolCatalog)


__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "LIST_TOOLS",
    "LoopSettings",
    "LoopState",
    "ModelRequest",
    "ToolCatalog",
    "ToolDescription",
    "ToolRegistration",
]
