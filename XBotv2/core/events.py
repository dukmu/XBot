"""XBot runtime events: the engine's extension points as XCore events.

The engine and tool layer dispatch these events on the XCore context;
plugins observe and intercept them with ``ctx.on(event, handler)``.  There is
no separate hook contract: short-circuit events are dispatched with
``ctx.serial`` (the first non-``None`` result is interpreted by the caller),
observer events with ``ctx.emit``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from XBotv2.core.context import ContextComponent
from XBotv2.core.messages import Message, ModelResponse
from XBotv2.core.runtime import SessionInfo
from XBotv2.core.tools import ToolCall


class Events:
    """Event names dispatched by the runtime (see module docstring)."""

    # Session lifecycle
    SESSION_INIT = "session/init"
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
    BEFORE_USER_MESSAGE_ACCEPT = "before/user-message-accept"
    AFTER_USER_MESSAGE_ACCEPT = "after/user-message-accept"
    # Context building
    BEFORE_CONTEXT = "before/context"
    PRE_COMPACT = "before/compact"
    POST_COMPACT = "after/compact"
    BEFORE_CONTEXT_BUILD = "before/context-build"
    AFTER_CONTEXT = "after/context"
    AFTER_CONTEXT_COMPONENTS_BUILD = "after/context-components-build"
    AFTER_CONTEXT_BUILD = "after/context-build"
    # Agent / model
    BEFORE_AGENT = "before/agent"
    BEFORE_TOOL_SCHEMA_BIND = "before/tool-schema-bind"
    AFTER_TOOL_SCHEMA_BIND = "after/tool-schema-bind"
    BEFORE_MODEL_REQUEST = "before/model-request"
    AFTER_MODEL_RESPONSE = "after/model-response"
    MODEL_REQUEST_ERROR = "model/request-error"
    AFTER_AGENT = "after/agent"
    # Tools
    BEFORE_TOOLS = "before/tools"
    AFTER_TOOLS = "after/tools"
    TOOL_CALLS_PARSED = "tool/calls-parsed"
    BEFORE_TOOL_CALL = "before/tool-call"
    AFTER_TOOL_CALL = "after/tool-call"
    TOOL_CALL_FAILURE = "tool/call-failure"
    TOOL_DENIED = "tool/denied"
    POST_TOOL_BATCH = "tool/batch-done"
    # Messages
    USER_MESSAGE = "user/message"
    ASSISTANT_MESSAGE = "assistant/message"
    TOOL_MESSAGE = "tool/message"
    # Permissions / client
    PERMISSION_REQUEST = "permission/request"
    PERMISSION_DENIED = "permission/denied"
    CLIENT_EVENT = "client/event"
    # Persistence
    BEFORE_STATE_PERSIST = "before/state-persist"
    AFTER_STATE_PERSIST = "after/state-persist"


#: Events dispatched with ``ctx.serial`` (first non-None result is the answer).
SHORT_CIRCUIT_EVENTS = frozenset({
    Events.BEFORE_USER_MESSAGE_ACCEPT,
    Events.BEFORE_CONTEXT,
    Events.PRE_COMPACT,
    Events.BEFORE_CONTEXT_BUILD,
    Events.AFTER_CONTEXT,
    Events.BEFORE_MODEL_REQUEST,
    Events.BEFORE_AGENT,
    Events.BEFORE_TOOL_SCHEMA_BIND,
    Events.AFTER_AGENT,
    Events.BEFORE_TOOLS,
    Events.BEFORE_TOOL_CALL,
    Events.AFTER_TOOLS,
})


class ToolAction(str, Enum):
    """Permission decision for a tool call (returned by before/tool-call)."""

    CONTINUE = "continue"
    ALLOW = "allow"
    DENY = "deny"
    STOP = "stop"


@dataclass(frozen=True)
class ToolDecision:
    """Decision returned by ``before/tool-call`` listeners."""

    action: ToolAction = ToolAction.CONTINUE
    reason: str = ""
    value: Any = None


@dataclass
class EventContext:
    """Payload object passed to runtime event listeners.

    Replaces the legacy hook context: the same state the runtime carries at
    each dispatch point, without a stage contract.
    """

    messages: list[Message] = field(default_factory=list)
    config: Any | None = None
    tools: Any | None = None
    sandbox: Any | None = None
    invoke_model: Callable[[list[Message]], Awaitable[ModelResponse]] | None = None
    request_user_input: Callable[..., Awaitable[dict[str, Any]]] | None = None
    request_continuation: Callable[[], Awaitable[None]] | None = None
    continuation: bool = False
    session: SessionInfo | None = None
    emit: Callable[[Any], None] = field(default=lambda _: None)
    user_input: str | None = None
    event: Any | None = None
    turn_complete: bool = False
    context_components: list[ContextComponent] | None = None
    context_messages: list[Any] | None = None
    agent_response: Any | None = None
    model_request: dict[str, Any] | None = None
    model_response: Any | None = None
    tool_calls: list[ToolCall] | None = None
    llm: Any | None = None
    tool_call: ToolCall | None = None
    args: dict[str, Any] | None = None
    tool_result: Any | None = None
    deny_reason: str | None = None
    tool_results: list[Any] | None = None
    reason: Any | None = None
    error: Any | None = None
    compact_reason: str | None = None
    compact_metrics: dict[str, Any] | None = None
    context_kwargs: dict[str, Any] | None = None
    previous_message_count: int | None = None
    current_message_count: int | None = None
    permission_decision: str | None = None
    client_event: dict[str, Any] | None = None
    stop_reason: str | None = None
    response: ModelResponse | None = None
    short_circuit_result: Any = None
    request_id: str = ""


__all__ = [
    "EventContext",
    "Events",
    "SHORT_CIRCUIT_EVENTS",
    "ToolAction",
    "ToolDecision",
]
