"""XBot runtime events: the engine's extension points as XCore events.

The engine and tool layer dispatch these events on the XCore context;
plugins observe and intercept them with ``ctx.on(event, handler)``.  There is
no separate hook contract: short-circuit events are dispatched with
``ctx.serial`` (the first non-``None`` result is interpreted by the caller),
observer events with ``ctx.emit``.
"""

from __future__ import annotations
from pydantic import BaseModel
from XBotv2.core.api.metadata import ThreadMetadata
from XBotv2.agentloop.api.state import ThreadExecution

from dataclasses import dataclass
from xcore.api.events import EventKey
from XBotv2.agentloop.api.state import ThreadExecution
from XBotv2.agentloop.api.contracts import InboxChange, InboxItem
from XBotv2.core.api.provider import ModelRequest, ProviderMessage
from XBotv2.agentloop.api.outputs import LoopEvent
from XBotv2.context_builder.api.events import ContextBuildRequest
from XBotv2.core.api.domain import ModelExchange
from XBotv2.core.api.messages import ConversationMessage
from XBotv2.core.api.stream import ModelResponse
from XBotv2.core.api.tools import ToolCall, ToolExecution


class Events:
    """Event names dispatched by the runtime (see module docstring)."""

    # Session lifecycle
    SESSION_START: EventKey[SessionLifecycle] = EventKey("session/start")
    SESSION_RESUME: EventKey[SessionLifecycle] = EventKey("session/resume")
    SESSION_CLOSE: EventKey[SessionLifecycle] = EventKey("session/close")
    # Turn lifecycle
    TURN_START: EventKey[TurnStarted] = EventKey("turn/start")
    TURN_END: EventKey[TurnEnded] = EventKey("turn/end")
    ON_ERROR: EventKey[LoopFailure] = EventKey("error")
    ON_STOP: EventKey[TurnEnded] = EventKey("stop")
    ON_STOP_FAILURE: EventKey[LoopFailure] = EventKey("stop/failure")
    # User input
    ON_TURN_INPUT: EventKey[OnTurnInput] = EventKey("input/received")
    INPUT_ACCEPTED: EventKey[InputAccepted] = EventKey("input/accepted")
    # Context building
    BEFORE_CONTEXT_BUILD: EventKey[BeforeContextBuild] = EventKey("context/before-build")
    AFTER_CONTEXT_BUILD: EventKey[AfterContextBuild] = EventKey("context/after-build")
    # Agent / model
    BEFORE_MODEL_REQUEST: EventKey[BeforeModelRequest] = EventKey("before/model-request")
    MODEL_REQUEST_READY: EventKey[ModelRequestReady] = EventKey("model/request-ready")
    AFTER_MODEL_RESPONSE: EventKey[AfterModelResponse] = EventKey("after/model-response")
    MODEL_RESPONSE_OBSERVED: EventKey[ModelResponseObserved] = EventKey("model/response-observed")
    MODEL_REQUEST_ERROR: EventKey[OnModelFailure] = EventKey("model/request-error")
    # Tools
    INBOX_CHANGED: EventKey[ObserveInbox] = EventKey("agent/inbox/changed")
    TOOL_CALLS_OBSERVED: EventKey[ToolCallsObserved] = EventKey("tool/calls-observed")
    BEFORE_TOOL_CALL: EventKey[BeforeToolCall] = EventKey("before/tool-call")
    AFTER_TOOL_CALL: EventKey[AfterToolExecution] = EventKey("after/tool-call")
    TOOL_BATCH_OBSERVED: EventKey[ToolBatchObserved] = EventKey("tool/batch-observed")
    TOOL_MESSAGE_OBSERVED: EventKey[ToolMessageObserved] = EventKey("tool/message-observed")
    # Permissions / client
    # Core state projection changed. Persistence is one possible observer;
    # the loop does not request or name storage operations.
    STATE_CHANGED: EventKey[StateChanged] = EventKey("state/changed")


@dataclass(frozen=True, slots=True)
class SessionLifecycle:
    """Synchronous lifecycle boundary; live state stays owned by LoopState."""

    pass


@dataclass(frozen=True, slots=True)
class TurnStarted:
    history: tuple[ConversationMessage, ...]


@dataclass(frozen=True, slots=True)
class TurnEnded:
    history: tuple[ConversationMessage, ...]
    stop_reason: str


@dataclass(frozen=True, slots=True)
class LoopFailure:
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


@dataclass(frozen=True, slots=True)
class RejectInput:
    error: str


@dataclass(frozen=True, slots=True)
class CompleteTurn:
    result: LoopEvent


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
    pass


@dataclass(frozen=True, slots=True)
class ReplaceContextRequest:
    request: ContextBuildRequest


BeforeContextBuildResult = KeepContextRequest | ReplaceContextRequest | CompleteTurn


@dataclass(frozen=True, slots=True)
class AfterContextBuild:
    context: tuple[ProviderMessage, ...]


@dataclass(frozen=True, slots=True)
class KeepContext:
    pass


@dataclass(frozen=True, slots=True)
class ReplaceContext:
    context: tuple[ProviderMessage, ...]


AfterContextBuildResult = KeepContext | ReplaceContext | CompleteTurn


@dataclass(frozen=True, slots=True)
class BeforeModelRequest:
    request: ModelRequest


@dataclass(frozen=True, slots=True)
class KeepRequest:
    pass


@dataclass(frozen=True, slots=True)
class ReplaceRequest:
    request: ModelRequest


BeforeModelRequestResult = KeepRequest | ReplaceRequest | CompleteTurn


@dataclass(frozen=True, slots=True)
class AfterModelResponse:
    request: ModelRequest
    response: ModelResponse


@dataclass(frozen=True, slots=True)
class KeepResponse:
    pass


@dataclass(frozen=True, slots=True)
class ReplaceResponse:
    response: ModelResponse


AfterModelResponseResult = KeepResponse | ReplaceResponse | CompleteTurn


@dataclass(frozen=True, slots=True)
class OnModelFailure:
    request: ModelRequest
    error: BaseException


@dataclass(frozen=True, slots=True)
class PropagateFailure:
    pass


@dataclass(frozen=True, slots=True)
class RetryRequest:
    request: ModelRequest


OnModelFailureResult = PropagateFailure | RetryRequest | CompleteTurn


@dataclass(frozen=True, slots=True)
class ModelRequestReady:
    request: ModelRequest
    session: ThreadExecution


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
    pass


@dataclass(frozen=True, slots=True)
class ReplaceToolCall:
    call: ToolCall


BeforeToolCallResult = KeepToolCall | ReplaceToolCall | CompleteTurn


@dataclass(frozen=True, slots=True)
class AfterToolExecution:
    execution: ToolExecution


@dataclass(frozen=True, slots=True)
class KeepExecution:
    pass


@dataclass(frozen=True, slots=True)
class ReplaceExecution:
    execution: ToolExecution


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
    Events.AFTER_MODEL_RESPONSE,
    Events.MODEL_REQUEST_ERROR,
    Events.BEFORE_TOOL_CALL,
    Events.AFTER_TOOL_CALL,
})

THREAD_INITIALIZED = "session/init"
RUNTIME_EVENT = "runtime/event"


@dataclass(frozen=True, slots=True)
class ThreadInitialized:
    session: ThreadExecution
    metadata: ThreadMetadata


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    event: BaseModel
