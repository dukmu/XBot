"""Typed operations owned by the Agent tool capability."""

from __future__ import annotations
from contextlib import AbstractAsyncContextManager
import asyncio
from XBotv2.core.api.operations import RequestT, ResponseT

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
import uuid
from typing import (
    TYPE_CHECKING,
    Annotated,
    Awaitable,
    Callable,
    Literal,
    Protocol,
    TypeAlias,
    overload,
)
from XBotv2.core.api.domain import InboxTarget, MessageId, TurnId
from XBotv2.core.api.operations import EmptyRequest, Operation
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from xcore.api.context import Context
from xcore.api.services import ServiceKey
from XBotv2.core.api.artifacts import ArtifactRef, ImageRef
from XBotv2.core.api.tools import GuardDecision, Tool, ToolCall, ToolExecution

if TYPE_CHECKING:
    from XBotv2.agentloop.api.models import (
        PendingInputData,
        SendMessage,
        HistoryMutation,
        SessionStatus,
    )
    from XBotv2.llm.api.contracts import ModelPort


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

    id: MessageId = Field(
        default_factory=lambda: MessageId(f"input-{uuid.uuid4().hex}"), min_length=1
    )
    target: InboxTarget
    input: InputPayload
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class Inserted:
    kind: Literal["inserted"] = field(default="inserted", kw_only=True)
    item: InboxItem
    wake: bool
    request_fingerprint: str = ""
    content_sha256: str = ""


@dataclass(frozen=True, slots=True)
class Edited:
    kind: Literal["edited"] = field(default="edited", kw_only=True)
    id: MessageId
    content: str
    artifacts: tuple[ArtifactRef, ...] = ()
    content_sha256: str = ""


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


InboxMutation: TypeAlias = (
    Inserted | Edited | Removed | Retargeted | Consumed | Discarded
)
InboxChange: TypeAlias = InboxMutation | Claimed


class InboxSink(Protocol):
    async def accepted(self, input_id: str, fingerprint: str) -> bool: ...
    async def current_content_sha256(self, input_id: str) -> str: ...
    async def load(self) -> list[InboxItem]: ...
    async def append(self, change: InboxMutation) -> bool: ...


class Inbox(Protocol):
    @property
    def pending(self) -> list[InboxItem]: ...
    def subscribe(self) -> asyncio.Event: ...
    def unsubscribe(self, observer: asyncio.Event) -> None: ...
    def __len__(self) -> int: ...
    async def submit(self, item: InboxItem, *, wake: bool) -> bool: ...
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


class AgentLoopPort(Protocol):
    @property
    def inbox(self) -> Inbox: ...
    @property
    def busy(self) -> bool: ...
    @property
    def execution_id(self) -> TurnId | None: ...
    @property
    def last_activity(self) -> float: ...
    @property
    def pending_input_count(self) -> int: ...
    def touch(self) -> None: ...
    def status(self) -> SessionStatus: ...
    def pending_inputs(self) -> tuple[PendingInputData, ...]: ...
    def resume_pending_inputs(self) -> bool: ...
    def idle(self, action: str) -> AbstractAsyncContextManager[None]: ...
    @overload
    def configure(self, *, model_client: ModelPort) -> None: ...
    @overload
    def configure(
        self, *, model_client: ModelPort, max_iterations: int | None
    ) -> None: ...
    async def invoke(
        self, operation: Operation[RequestT, ResponseT], request: RequestT
    ) -> ResponseT: ...
    async def interrupt(self) -> bool: ...
    async def submit_input(self, item: InboxItem, *, wake: bool) -> None: ...
    async def submit_message(self, request: SendMessage) -> None: ...
    async def send_message(
        self,
        content: str,
        request_id: str,
        *,
        delivery: str = "steer",
        images: list[ImageRef] | None = None,
        artifacts: list[ArtifactRef] | None = None,
    ) -> None: ...
    async def update_pending_input(
        self, message_id: str, action: str, content: str = ""
    ) -> tuple[PendingInputData, ...]: ...
    async def regenerate(self, request_id: str) -> None: ...
    async def clear_history(
        self, *, history_limit: int | None = None
    ) -> HistoryMutation: ...
    async def undo_history(
        self, count: int, *, history_limit: int | None = None
    ) -> HistoryMutation: ...


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
        self,
        guard: ToolGuard,
        *,
        approval: bool = False,
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


LIST_TOOLS = Operation("tools/list", EmptyRequest, ToolCatalog)


TOOLS: ServiceKey[ToolsPort] = ServiceKey("tools")
INBOX: ServiceKey[Inbox] = ServiceKey("agent_inbox")
AGENT_LOOP: ServiceKey[AgentLoopPort] = ServiceKey("agent_loop")
