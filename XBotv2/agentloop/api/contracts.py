"""Typed operations owned by the Agent tool capability."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
import uuid
from typing import TYPE_CHECKING, Annotated, Awaitable, Callable, Literal, Protocol, TypeAlias
from XBotv2.core.api.domain import InboxTarget, MessageId
from XBotv2.core.api.operations import EmptyRequest, Operation
from XBotv2.core.api.messages import ConversationMessage
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from xcore.api.context import Context
from xcore.api.services import ServiceKey
from XBotv2.core.api.artifacts import ArtifactRef, ImageRef
from XBotv2.core.api.tools import GuardDecision, Tool, ToolCall, ToolExecution
from XBotv2.agentloop.api.state import LoopState

if TYPE_CHECKING:
    from XBotv2.agentloop.api.outputs import LoopEvent
    from xcore.api.events import EventDispatch

    from XBotv2.llm.api.contracts import ModelPort
    from XBotv2.core.api.domain import UserContext

DEFAULT_MAX_ITERATIONS = 200


class AllTools(BaseModel):
    """Select every registered model-visible tool."""

    kind: Literal["all"] = "all"
    model_config = ConfigDict(extra="forbid", frozen=True)


ToolSelection: TypeAlias = tuple[str, ...] | AllTools


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

    id: MessageId = Field(default_factory=lambda: MessageId(f"input-{uuid.uuid4().hex}"), min_length=1)
    target: InboxTarget
    input: InputPayload
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class Inserted:
    kind: Literal["inserted"] = field(default="inserted", kw_only=True)
    item: InboxItem
    wake: bool


@dataclass(frozen=True, slots=True)
class Edited:
    kind: Literal["edited"] = field(default="edited", kw_only=True)
    id: MessageId
    content: str


@dataclass(frozen=True, slots=True)
class Removed:
    kind: Literal["removed"] = field(default="removed", kw_only=True)
    id: MessageId


@dataclass(frozen=True, slots=True)
class Retargeted:
    kind: Literal["retargeted"] = field(default="retargeted", kw_only=True)
    id: MessageId
    target: InboxTarget


@dataclass(frozen=True, slots=True)
class Claimed:
    kind: Literal["claimed"] = field(default="claimed", init=False)
    ids: tuple[MessageId, ...]


@dataclass(frozen=True, slots=True)
class Consumed:
    kind: Literal["consumed"] = field(default="consumed", kw_only=True)
    ids: tuple[MessageId, ...]


@dataclass(frozen=True, slots=True)
class Discarded:
    kind: Literal["discarded"] = field(default="discarded", kw_only=True)
    ids: tuple[MessageId, ...]


InboxMutation: TypeAlias = Inserted | Edited | Removed | Retargeted | Consumed | Discarded
InboxChange: TypeAlias = InboxMutation | Claimed


class InboxSink(Protocol):
    def append(self, change: InboxMutation) -> None: ...


class Inbox(Protocol):
    @property
    def pending(self) -> list[InboxItem]: ...
    def __len__(self) -> int: ...
    async def submit(self, item: InboxItem, *, wake: bool) -> None: ...
    async def claim_turn(self) -> list[InboxItem]: ...
    async def claim_step(self) -> list[InboxItem]: ...
    async def commit(self, input_ids: Sequence[str]) -> None: ...
    async def reconcile(self, committed_ids: set[str]) -> None: ...
    async def edit(self, input_id: str, content: str) -> InboxItem: ...
    async def remove(self, input_id: str) -> InboxItem: ...
    async def retarget(self, input_id: str, target: InboxTarget | str) -> InboxItem: ...
    async def discard(self) -> list[InboxItem]: ...


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
    events: EventDispatch
    state: LoopState
    user_identity: "UserContext"
    memory: str
    max_iterations: int
    #: The composed inbox (durable after hydration, transient otherwise),
    #: bound at construction by the agent_inbox service dependency.
    inbox: "Inbox"


class AgentLoopDriverPort(Protocol):
    messages: Sequence[ConversationMessage]
    pending_input_count: int
    pending_inputs: Sequence[InboxItem]

    def configure(
        self, *, model_client: ModelPort, max_iterations: int | None = None,
    ) -> None: ...

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
        owner: Context | None = None,
        cleanup: Literal["fiber", "caller"] | None = None,
    ) -> str: ...
    def unregister(self, name: str) -> bool: ...
    def guard(
        self, guard: ToolGuard, *, approval: bool = False,
        owner: Context | None = None,
        cleanup: Literal["fiber", "caller"] | None = None,
    ) -> bool: ...
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


TOOLS: ServiceKey[ToolsPort] = ServiceKey("tools")
INBOX: ServiceKey[Inbox] = ServiceKey("agent_inbox")
AGENT_LOOP_FACTORY: ServiceKey[AgentLoopFactoryPort] = ServiceKey("agent_loop_factory")
