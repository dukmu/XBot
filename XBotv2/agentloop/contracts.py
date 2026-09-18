"""Typed operations owned by the Agent tool capability."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import uuid
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Protocol, TypedDict

from XBotv2.core.history import ConversationHistory
from XBotv2.core.variables import RuntimeVariables
from XBotv2.core.operations import EmptyRequest, Operation
from XBotv2.core.messages import RUNTIME_INPUT_KEY, ImageContent, Message
from XBotv2.core.metadata import ThreadMetadata, ThreadMetadataState
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from xcore import Context
from xcore.service import Service

from XBotv2.core.artifacts import ArtifactRef
from XBotv2.core.tools import GuardDecision, Tool, ToolCall
from XBotv2.session.contracts import SessionInfo

if TYPE_CHECKING:
    from XBotv2.agentloop.events import EventContext, EventPort
    from XBotv2.agentloop.inbox import AgentInbox
    from XBotv2.llm.contracts import ModelPort

DEFAULT_MAX_ITERATIONS = 200
class InboxTarget(str, Enum):
    NEXT_TURN = "next-turn"
    NEXT_STEP = "next-step"


class InboxInput(BaseModel):
    """One uniquely identified model-visible input."""

    content: str
    target: InboxTarget
    source: str = "user"
    message_id: str = Field(default_factory=lambda: f"msg-{uuid.uuid4().hex}")
    images: list[ImageContent] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")


class InboxSplice(BaseModel):
    """Typed lifecycle record emitted by the agent inbox."""

    operation: Literal[
        "insert", "edit", "remove", "retarget", "claim", "consume", "discard"
    ]
    target: InboxTarget | None = None
    message_ids: list[str] = Field(default_factory=list)
    items: list[InboxInput] = Field(default_factory=list)
    #: The producer's wake intent: the owning session runtime wakes the loop
    #: for a waking splice and stays idle for a non-waking one (inject).
    wake: bool = False
    model_config = ConfigDict(extra="forbid", frozen=True)


class InboxSink(Protocol):
    def replace(self, items: Sequence[InboxInput]) -> None: ...


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
        session: SessionInfo,
        messages: list[Message] | ConversationHistory | None = None,
        turn_count: int = 0,
        resumed: bool = False,
        metadata: ThreadMetadata | dict[str, JsonValue] | None = None,
        variables: RuntimeVariables = RuntimeVariables(),
    ) -> None:
        super().__init__(ctx, name=self.name)
        self.session = session
        self.variables = variables
        self.history = (
            messages
            if isinstance(messages, ConversationHistory)
            else ConversationHistory(messages or ())
        )
        self._turn_count = 0
        self.turn_count = turn_count
        self.resumed = resumed
        self.metadata = ThreadMetadataState(
            ctx,
            session_id=session.session_id,
            thread_id=session.thread_id,
            value=(
                metadata
                if isinstance(metadata, ThreadMetadata)
                else ThreadMetadata.from_state(metadata or {})
            ),
        )

    @property
    def messages(self) -> ConversationHistory:
        return self.history

    @property
    def turn_count(self) -> int:
        return self._turn_count

    @turn_count.setter
    def turn_count(self, value: int) -> None:
        # One counter, one mirror: the session projection follows the state.
        self._turn_count = value
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

    def set_provider(self, provider: str) -> None:
        """Update the session-identity projection of the active provider.

        The authoritative provider lives in thread metadata (announced on the
        bus); this mirror exists for readers that hold only the session
        identity, and is written through one named transition.
        """
        self.session.provider = provider

    def set_history(self, history: ConversationHistory) -> None:
        self.history = history

    def replace_messages(self, messages: list[Message]) -> None:
        self.history.replace(messages, operation="replace")

    def replace_message_range(
        self,
        start: int,
        end: int,
        messages: list[Message],
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


class LoopSettingsUpdate(TypedDict, total=False):
    """Keyword fields accepted when reconfiguring an active loop."""

    provider: str
    model: str
    model_mode: str
    context_window: int
    max_output_tokens: int
    agent_name: str
    agent_role: str
    user_name: str
    user_id: str
    developer_instructions: str
    agent_instructions: str
    memory: str
    workspace: str
    llm_is_override: bool


@dataclass(slots=True)
class ModelRequest:
    """Mutable provider request exposed to Agent-loop event listeners."""

    messages: list[Message]
    tools: list[Tool]
    llm: ModelPort


class ModelRequestErrorOutcome(BaseModel):
    """What a ``model/request-error`` listener asks the loop to do next."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    retry: bool = False


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
    settings: LoopSettings
    max_iterations: int
    #: The composed inbox (durable after hydration, transient otherwise),
    #: bound at construction by the agent_inbox service dependency.
    inbox: "AgentInbox"


class AgentLoopDriverPort(Protocol):
    settings: LoopSettings
    messages: Sequence[Message]
    context_window: int
    pending_input_count: int
    pending_inputs: Sequence[InboxInput]

    async def start_session(self) -> None: ...
    async def close_session(self) -> None: ...
    async def discard_inputs(self) -> None: ...
    async def followup(
        self,
        content: str,
        *,
        source: str = "user",
        message_id: str = "",
        images: list[ImageContent] | None = None,
        artifacts: list[ArtifactRef] | None = None,
        metadata: dict[str, JsonValue] | None = None,
    ) -> InboxInput: ...
    async def inject(
        self,
        content: str,
        *,
        source: str = "user",
        message_id: str = "",
        images: list[ImageContent] | None = None,
        artifacts: list[ArtifactRef] | None = None,
        metadata: dict[str, JsonValue] | None = None,
    ) -> InboxInput: ...
    async def steer(
        self,
        content: str,
        *,
        source: str = "user",
        message_id: str = "",
        images: list[ImageContent] | None = None,
        artifacts: list[ArtifactRef] | None = None,
        metadata: dict[str, JsonValue] | None = None,
    ) -> InboxInput: ...
    async def edit_input(self, message_id: str, content: str) -> InboxInput: ...
    async def remove_input(self, message_id: str) -> InboxInput: ...
    async def retarget_input(
        self,
        message_id: str,
        target: InboxTarget,
    ) -> InboxInput: ...
    def run_turn(
        self,
        content: str,
        *,
        request_id: str = "",
        images: list[ImageContent] | None = None,
        artifacts: list[ArtifactRef] | None = None,
    ) -> AsyncIterator[dict[str, JsonValue]]: ...
    def run_pending(
        self,
        *,
        request_id: str = "",
    ) -> AsyncIterator[dict[str, JsonValue]]: ...


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
    def restrict(self, selectors: list[str] | None) -> tuple[str, ...]: ...
    def exclude(self, selectors: list[str]) -> tuple[str, ...]: ...
    async def execute_all(
        self,
        tool_calls: list[ToolCall],
        *,
        context_factory: Callable[..., EventContext] | None = None,
    ) -> list[Message]: ...
    def execute_each(
        self,
        tool_calls: list[ToolCall],
        *,
        context_factory: Callable[..., EventContext] | None = None,
    ) -> AsyncIterator[Message]: ...


class AgentLoopFactoryPort(Protocol):
    def create(self, options: LoopFactoryOptions) -> AgentLoopDriverPort: ...


LIST_TOOLS = Operation("tools/list", EmptyRequest, ToolCatalog)


#: Additional-kwarg key carrying display provenance for injected turns.
def runtime_input_value(
    source: str,
    metadata: dict[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    """Build the persisted display provenance for a non-human user message."""
    if source == "user":
        return {}
    values = metadata or {}
    event = values.get("kind")
    if not isinstance(event, str) or not event:
        event = "continuation" if values.get("continuation") else "injected"
    return {RUNTIME_INPUT_KEY: {"source": source, "event": event}}


def runtime_input_labels(
    runtime: Mapping[str, object] | None,
) -> tuple[str, str] | None:
    """Read the (source, event) labels of an injected turn.

    Returns ``None`` when the persisted shape does not carry both labels, so
    consumers never invent a provenance that was not recorded.
    """
    if not isinstance(runtime, Mapping):
        return None
    source = runtime.get("source")
    event = runtime.get("event")
    if not isinstance(source, str) or not source:
        return None
    if not isinstance(event, str) or not event:
        return None
    return source, event


__all__ = [
    "RUNTIME_INPUT_KEY",
    "runtime_input_labels",
    "runtime_input_value",
    "AgentLoopDriverPort",
    "AgentLoopFactoryPort",
    "DEFAULT_MAX_ITERATIONS",
    "InboxInput",
    "InboxSink",
    "InboxTarget",
    "InboxSplice",
    "LIST_TOOLS",
    "LoopFactoryOptions",
    "LoopSettings",
    "LoopState",
    "ModelRequest",
    "ModelRequestErrorOutcome",
    "ToolCatalog",
    "ToolDescription",
    "ToolGuard",
    "ToolRegistration",
    "ToolsPort",
]
