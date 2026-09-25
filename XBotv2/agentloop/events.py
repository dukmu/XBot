"""XBot runtime events: the engine's extension points as XCore events.

The engine and tool layer dispatch these events on the XCore context;
plugins observe and intercept them with ``ctx.on(event, handler)``.  There is
no separate hook contract: short-circuit events are dispatched with
``ctx.serial`` (the first non-``None`` result is interpreted by the caller),
observer events with ``ctx.emit``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from XBotv2.agentloop.contracts import InboxChange, InboxItem
from XBotv2.core.provider import ModelRequest
from XBotv2.agentloop.protocol import LoopEvent
from XBotv2.context_builder.events import ContextBuildRequest
from XBotv2.core.domain import ModelExchange
from XBotv2.core.messages import ConversationMessage
from XBotv2.core.provider import ProviderMessage
from XBotv2.core.stream import ModelResponse
from XBotv2.core.tools import ToolCall, ToolExecution
from XBotv2.session.contracts import SessionRuntimeState


class Events:
    """Event names dispatched by the runtime (see module docstring)."""

    # Session lifecycle
    SESSION_START = "session/start"
    SESSION_RESUME = "session/resume"
    SESSION_CLOSE = "session/close"
    # Turn lifecycle
    TURN_START = "turn/start"
    TURN_END = "turn/end"
    ON_ERROR = "error"
    ON_STOP = "stop"
    ON_STOP_FAILURE = "stop/failure"
    # User input
    ON_TURN_INPUT = "input/received"
    INPUT_ACCEPTED = "input/accepted"
    # Context building
    BEFORE_CONTEXT_BUILD = "context/before-build"
    AFTER_CONTEXT_BUILD = "context/after-build"
    # Agent / model
    BEFORE_MODEL_REQUEST = "before/model-request"
    MODEL_REQUEST_READY = "model/request-ready"
    AFTER_MODEL_RESPONSE = "after/model-response"
    MODEL_RESPONSE_OBSERVED = "model/response-observed"
    MODEL_REQUEST_ERROR = "model/request-error"
    # Tools
    INBOX_CHANGED = "agent/inbox/changed"
    TOOL_CALLS_OBSERVED = "tool/calls-observed"
    BEFORE_TOOL_CALL = "before/tool-call"
    AFTER_TOOL_CALL = "after/tool-call"
    TOOL_BATCH_OBSERVED = "tool/batch-observed"
    TOOL_MESSAGE_OBSERVED = "tool/message-observed"
    # Permissions / client
    # Core state projection changed. Persistence is one possible observer;
    # the loop does not request or name storage operations.
    STATE_CHANGED = "state/changed"


class EventPort(Protocol):
    """Narrow event surface consumed by the concrete loop driver."""

    async def emit(self, event: str, *args: Any) -> Any: ...

    async def serial(self, event: str, *args: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class SessionLifecycle:
    session: SessionRuntimeState


@dataclass(frozen=True, slots=True)
class TurnStarted:
    session: SessionRuntimeState
    history: tuple[ConversationMessage, ...]


@dataclass(frozen=True, slots=True)
class TurnEnded:
    session: SessionRuntimeState
    history: tuple[ConversationMessage, ...]
    stop_reason: str


@dataclass(frozen=True, slots=True)
class LoopFailure:
    session: SessionRuntimeState
    history: tuple[ConversationMessage, ...]
    error: BaseException


@dataclass(frozen=True, slots=True)
class StateChanged:
    pass


@dataclass(frozen=True, slots=True)
class ObserveInbox:
    change: InboxChange


@dataclass(frozen=True, slots=True)
class OnTurnInput:
    input: InboxItem
    history: tuple[ConversationMessage, ...]


@dataclass(frozen=True, slots=True)
class AcceptInput:
    input: InboxItem
    kind: str = "accept"


@dataclass(frozen=True, slots=True)
class RejectInput:
    error: str
    kind: str = "reject"


@dataclass(frozen=True, slots=True)
class CompleteTurn:
    result: LoopEvent
    kind: str = "complete"


OnTurnInputResult = AcceptInput | RejectInput | CompleteTurn


@dataclass(frozen=True, slots=True)
class InputAccepted:
    """Accepted input and its canonical candidate before history append."""

    input: InboxItem
    message: ConversationMessage


@dataclass(frozen=True, slots=True)
class BeforeContextBuild:
    request: ContextBuildRequest


@dataclass(frozen=True, slots=True)
class KeepContextRequest:
    kind: str = "keep"


@dataclass(frozen=True, slots=True)
class ReplaceContextRequest:
    request: ContextBuildRequest
    kind: str = "replace"


BeforeContextBuildResult = KeepContextRequest | ReplaceContextRequest | CompleteTurn


@dataclass(frozen=True, slots=True)
class AfterContextBuild:
    context: tuple[ProviderMessage, ...]


@dataclass(frozen=True, slots=True)
class KeepContext:
    kind: str = "keep"


@dataclass(frozen=True, slots=True)
class ReplaceContext:
    context: tuple[ProviderMessage, ...]
    kind: str = "replace"


AfterContextBuildResult = KeepContext | ReplaceContext | CompleteTurn


@dataclass(frozen=True, slots=True)
class BeforeModelRequest:
    request: ModelRequest


@dataclass(frozen=True, slots=True)
class KeepRequest:
    kind: str = "keep"


@dataclass(frozen=True, slots=True)
class ReplaceRequest:
    request: ModelRequest
    kind: str = "replace"


BeforeModelRequestResult = KeepRequest | ReplaceRequest | CompleteTurn


@dataclass(frozen=True, slots=True)
class AfterModelResponse:
    request: ModelRequest
    response: ModelResponse


@dataclass(frozen=True, slots=True)
class KeepResponse:
    kind: str = "keep"


@dataclass(frozen=True, slots=True)
class ReplaceResponse:
    response: ModelResponse
    kind: str = "replace"


AfterModelResponseResult = KeepResponse | ReplaceResponse | CompleteTurn


@dataclass(frozen=True, slots=True)
class OnModelFailure:
    request: ModelRequest
    error: BaseException


@dataclass(frozen=True, slots=True)
class PropagateFailure:
    kind: str = "propagate"


@dataclass(frozen=True, slots=True)
class RetryRequest:
    request: ModelRequest
    kind: str = "retry"


OnModelFailureResult = PropagateFailure | RetryRequest | CompleteTurn


@dataclass(frozen=True, slots=True)
class ModelRequestReady:
    request: ModelRequest
    session: SessionRuntimeState


@dataclass(frozen=True, slots=True)
class ModelResponseObserved:
    exchange: ModelExchange


@dataclass(frozen=True, slots=True)
class ToolCallsObserved:
    calls: tuple[ToolCall, ...]


@dataclass(frozen=True, slots=True)
class ToolMessageObserved:
    message: ConversationMessage


@dataclass(frozen=True, slots=True)
class BeforeToolCall:
    call: ToolCall


@dataclass(frozen=True, slots=True)
class KeepToolCall:
    kind: str = "keep"


@dataclass(frozen=True, slots=True)
class ReplaceToolCall:
    call: ToolCall
    kind: str = "replace"


BeforeToolCallResult = KeepToolCall | ReplaceToolCall | CompleteTurn


@dataclass(frozen=True, slots=True)
class AfterToolExecution:
    execution: ToolExecution


@dataclass(frozen=True, slots=True)
class KeepExecution:
    kind: str = "keep"


@dataclass(frozen=True, slots=True)
class ReplaceExecution:
    execution: ToolExecution
    kind: str = "replace"


AfterToolExecutionResult = KeepExecution | ReplaceExecution | CompleteTurn


@dataclass(frozen=True, slots=True)
class ToolBatchObserved:
    calls: tuple[ToolCall, ...]
    executions: tuple[ToolExecution, ...]


#: Events dispatched with ``ctx.serial`` (first non-None result is the answer).
SHORT_CIRCUIT_EVENTS = frozenset({
    Events.ON_TURN_INPUT,
    Events.INPUT_ACCEPTED,
    Events.BEFORE_CONTEXT_BUILD,
    Events.AFTER_CONTEXT_BUILD,
    Events.BEFORE_MODEL_REQUEST,
    Events.MODEL_REQUEST_ERROR,
    Events.BEFORE_TOOL_CALL,
    Events.AFTER_TOOL_CALL,
})


__all__ = [
    "AcceptInput",
    "AfterContextBuild",
    "AfterContextBuildResult",
    "AfterModelResponse",
    "AfterModelResponseResult",
    "EventPort",
    "Events",
    "SHORT_CIRCUIT_EVENTS",
    "AfterToolExecution",
    "AfterToolExecutionResult",
    "BeforeToolCall",
    "BeforeToolCallResult",
    "BeforeContextBuild",
    "BeforeContextBuildResult",
    "BeforeModelRequest",
    "BeforeModelRequestResult",
    "CompleteTurn",
    "KeepExecution",
    "InputAccepted",
    "KeepContext",
    "KeepContextRequest",
    "KeepRequest",
    "KeepResponse",
    "KeepToolCall",
    "ReplaceExecution",
    "ReplaceContext",
    "ReplaceContextRequest",
    "ReplaceRequest",
    "ReplaceResponse",
    "ReplaceToolCall",
    "ToolBatchObserved",
    "LoopFailure",
    "ModelRequestReady",
    "ModelResponseObserved",
    "ObserveInbox",
    "OnModelFailure",
    "OnModelFailureResult",
    "OnTurnInput",
    "OnTurnInputResult",
    "PropagateFailure",
    "RejectInput",
    "RetryRequest",
    "SessionLifecycle",
    "StateChanged",
    "ToolCallsObserved",
    "ToolMessageObserved",
    "TurnEnded",
    "TurnStarted",
]
