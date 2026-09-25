"""Typed operations owned by the Agent tool capability."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import uuid
from typing import TYPE_CHECKING, Annotated, Awaitable, Callable, Literal, Protocol, TypeAlias

from XBotv2.core.history import ConversationHistory
from XBotv2.core.variables import RuntimeVariables
from XBotv2.core.operations import EmptyRequest, Operation
from XBotv2.core.messages import ConversationMessage
from XBotv2.core.metadata import ThreadMetadataState
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from xcore import Context
from xcore.service import Service

from XBotv2.core.artifacts import ArtifactRef, ImageRef
from XBotv2.core.tools import (
    GuardDecision,
    Tool,
    ToolCall,
    ToolExecution,
)
from XBotv2.session.contracts import SessionKey, SessionRuntimeState

if TYPE_CHECKING:
    from XBotv2.agentloop.protocol import LoopEvent
    from XBotv2.agentloop.events import EventPort
    from XBotv2.agentloop.inbox import AgentInbox
    from XBotv2.llm.contracts import ModelPort
    from XBotv2.config.contracts import UserContext

DEFAULT_MAX_ITERATIONS = 200


class AllTools(BaseModel):
    """Select every registered model-visible tool."""

    kind: Literal["all"] = "all"
    model_config = ConfigDict(extra="forbid", frozen=True)


ToolSelection: TypeAlias = tuple[str, ...] | AllTools
class InboxTarget(str, Enum):
    NEXT_TURN = "next-turn"
    NEXT_STEP = "next-step"


class HumanInput(BaseModel):
    kind: Literal["human"] = "human"
    content: str
    images: tuple[ImageRef, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeInput(BaseModel):
    kind: Literal["runtime"] = "runtime"
    source: str = Field(min_length=1)
    event: str = Field(min_length=1)
    content: str
    images: tuple[ImageRef, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    model_config = ConfigDict(extra="forbid", frozen=True)


InputPayload = Annotated[
    HumanInput | RuntimeInput,
    Field(discriminator="kind"),
]


class InboxItem(BaseModel):
    """One uniquely identified model-visible input."""

    id: str = Field(default_factory=lambda: f"input-{uuid.uuid4().hex}")
    target: InboxTarget
    input: InputPayload
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class Inserted:
    kind: Literal["inserted"] = field(default="inserted", init=False)
    item: InboxItem
    wake: bool


@dataclass(frozen=True, slots=True)
class Edited:
    kind: Literal["edited"] = field(default="edited", init=False)
    previous: InboxItem
    current: InboxItem


@dataclass(frozen=True, slots=True)
class Removed:
    kind: Literal["removed"] = field(default="removed", init=False)
    item: InboxItem


@dataclass(frozen=True, slots=True)
class Retargeted:
    kind: Literal["retargeted"] = field(default="retargeted", init=False)
    previous: InboxItem
    current: InboxItem


@dataclass(frozen=True, slots=True)
class Claimed:
    kind: Literal["claimed"] = field(default="claimed", init=False)
    items: tuple[InboxItem, ...]


@dataclass(frozen=True, slots=True)
class Consumed:
    kind: Literal["consumed"] = field(default="consumed", init=False)
    items: tuple[InboxItem, ...]


@dataclass(frozen=True, slots=True)
class Discarded:
    kind: Literal["discarded"] = field(default="discarded", init=False)
    items: tuple[InboxItem, ...]


InboxChange: TypeAlias = (
    Inserted | Edited | Removed | Retargeted | Claimed | Consumed | Discarded
)


class InboxSink(Protocol):
    def replace(self, items: Sequence[InboxItem]) -> None: ...


class LoopState(Service):
    """Mutable conversation state owned by the Agent loop.

    Constructing the state provides it on the context as ``loop_state`` (and
    constructs the thread's ``thread_metadata``) for the owning fiber's
    lifetime; nobody rebinds either afterwards. The durable inbox is composed
    separately and handed to the loop driver at construction.

    ``turn_count`` is a lifetime counter with one owner (the loop driver): it
    increments when a turn is accepted and is preserved across compaction and
    history edits, exactly like ``SessionStats``. History mutations never
    recompute it; hydration restores it explicitly from the durable surface.
    """

    name = "loop_state"

    def __init__(
        self,
        ctx: Context,
        *,
        key: SessionKey,
        messages: list[ConversationMessage] | ConversationHistory | None = None,
        turn_count: int = 0,
        resumed: bool = False,
        variables: RuntimeVariables = RuntimeVariables(),
    ) -> None:
        super().__init__(ctx, name=self.name)
        self.variables = variables
        self.history = (
            messages
            if isinstance(messages, ConversationHistory)
            else ConversationHistory(messages or ())
        )
        self.metadata = ThreadMetadataState(
            ctx,
            session_id=key.session_id,
            thread_id=key.thread_id,
        )
        self.session = SessionRuntimeState(key=key, metadata=self.metadata)
        self.turn_count = turn_count
        self.resumed = resumed

    @property
    def messages(self) -> ConversationHistory:
        return self.history

    @property
    def turn_count(self) -> int:
        return self.session.turn_count

    @turn_count.setter
    def turn_count(self, value: int) -> None:
        self.session.turn_count = value

    def restore_turn_count(self, count: int) -> None:
        """Restore the lifetime counter from the durable surface (hydration).

        Only the loop driver increments it; hydration restores it once on a
        resumed session.
        """
        self.turn_count = count

    def restore_resumed(self, resumed: bool) -> None:
        """Record whether this session resumed durable state (hydration)."""
        self.resumed = resumed

    def set_history(self, history: ConversationHistory) -> None:
        self.history = history

    def replace_messages(self, messages: list[ConversationMessage]) -> None:
        self.history.replace(messages, operation="replace")

    def replace_message_range(
        self,
        start: int,
        end: int,
        messages: list[ConversationMessage],
        *,
        operation: str,
        preserve_transcript: bool = False,
    ) -> None:
        self.history.replace_range(
            start,
            end,
            messages,
            operation=operation,
            preserve_transcript=preserve_transcript,
        )


@dataclass(frozen=True, slots=True)
class ToolDescription:
    name: str
    registered_name: str
    namespace: str
    description: str
    parameters: dict[str, JsonValue]
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


@dataclass(frozen=True, slots=True)
class LoopFactoryOptions:
    """Resolved ports consumed by an Agent loop factory."""

    model_client: ModelPort
    tools: "ToolsPort"
    events: EventPort
    state: LoopState
    user_identity: "UserContext"
    memory: str
    max_iterations: int
    #: The composed inbox (durable after hydration, transient otherwise),
    #: bound at construction by the agent_inbox service dependency.
    inbox: "AgentInbox"


class AgentLoopDriverPort(Protocol):
    messages: Sequence[ConversationMessage]
    pending_input_count: int
    pending_inputs: Sequence[InboxItem]

    async def start_session(self) -> None: ...
    async def close_session(self) -> None: ...
    async def discard_inputs(self) -> None: ...
    async def submit_input(self, item: InboxItem, *, wake: bool) -> None: ...
    async def edit_input(self, input_id: str, content: str) -> InboxItem: ...
    async def remove_input(self, input_id: str) -> InboxItem: ...
    async def retarget_input(
        self,
        input_id: str,
        target: InboxTarget,
    ) -> InboxItem: ...
    def run_turn(
        self,
        item: InboxItem,
        *,
        request_id: str = "",
    ) -> AsyncIterator[LoopEvent]: ...
    def run_pending(
        self,
        *,
        request_id: str = "",
    ) -> AsyncIterator[LoopEvent]: ...


ToolGuard = Callable[
    [ToolCall, ToolRegistration],
    GuardDecision | None | Awaitable[GuardDecision | None],
]


class ToolsPort(Protocol):
    def register(
        self,
        tool: Tool,
        *,
        model_visible: bool = True,
        timeout_seconds: float | None = None,
        namespace: str | None = None,
    ) -> str: ...
    def unregister(self, name: str) -> bool: ...
    def guard(self, guard: ToolGuard) -> bool: ...
    def enabled(self) -> tuple[Tool, ...]: ...
    def resolve(self, name: str, *, include_disabled: bool = False) -> Tool | None: ...
    def names(self) -> tuple[str, ...]: ...
    def registered_names(self) -> tuple[str, ...]: ...
    def registrations(self) -> tuple[ToolRegistration, ...]: ...
    def restrict(self, selection: ToolSelection) -> tuple[str, ...]: ...
    def exclude(self, selectors: list[str]) -> tuple[str, ...]: ...
    async def execute_all(
        self,
        tool_calls: list[ToolCall],
    ) -> list[ToolExecution]: ...
    def execute_each(
        self,
        tool_calls: list[ToolCall],
    ) -> AsyncIterator[ToolExecution]: ...


class AgentLoopFactoryPort(Protocol):
    def create(self, options: LoopFactoryOptions) -> AgentLoopDriverPort: ...


LIST_TOOLS = Operation("tools/list", EmptyRequest, ToolCatalog)


__all__ = [
    "HumanInput",
    "RuntimeInput",
    "InputPayload",
    "AgentLoopDriverPort",
    "AgentLoopFactoryPort",
    "AllTools",
    "DEFAULT_MAX_ITERATIONS",
    "InboxItem",
    "InboxSink",
    "InboxTarget",
    "InboxChange",
    "Inserted",
    "Edited",
    "Removed",
    "Retargeted",
    "Claimed",
    "Consumed",
    "Discarded",
    "LIST_TOOLS",
    "LoopFactoryOptions",
    "LoopState",
    "ToolCatalog",
    "ToolDescription",
    "ToolGuard",
    "ToolRegistration",
    "ToolSelection",
    "ToolsPort",
]
