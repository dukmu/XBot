"""XBot runtime events: the engine's extension points as XCore events.

The engine and tool layer dispatch these events on the XCore context;
plugins observe and intercept them with ``ctx.on(event, handler)``.  There is
no separate hook contract: short-circuit events are dispatched with
``ctx.serial`` (the first non-``None`` result is interpreted by the caller),
observer events with ``ctx.emit``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from XBotv2.agentloop.contracts import LoopSettings, ModelRequest
from XBotv2.core.messages import Message, ModelResponse
from XBotv2.core.tools import ClientEvent, ToolCall
from XBotv2.session import SessionInfo


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
    BEFORE_USER_MESSAGE_ACCEPT = "before/user-message-accept"
    AFTER_USER_MESSAGE_ACCEPT = "after/user-message-accept"
    # Context building
    BEFORE_CONTEXT = "before/context"
    AFTER_CONTEXT = "after/context"
    # Agent / model
    BEFORE_AGENT = "before/agent"
    BEFORE_TOOL_SCHEMA_BIND = "before/tool-schema-bind"
    AFTER_TOOL_SCHEMA_BIND = "after/tool-schema-bind"
    BEFORE_MODEL_REQUEST = "before/model-request"
    MODEL_REQUEST_READY = "model/request-ready"
    AFTER_MODEL_RESPONSE = "after/model-response"
    MODEL_REQUEST_ERROR = "model/request-error"
    AFTER_AGENT = "after/agent"
    # Tools
    BEFORE_TOOLS = "before/tools"
    AFTER_TOOLS = "after/tools"
    INBOX_SPLICE = "agent/inbox/spliced"
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
    CLIENT_EVENT = "client/event"
    # Core state projection changed. Persistence is one possible observer;
    # the loop does not request or name storage operations.
    STATE_CHANGED = "state/changed"


class EventPort(Protocol):
    """Narrow event surface consumed by the concrete loop driver."""

    async def emit(self, event: str, *args: Any) -> Any: ...

    async def serial(self, event: str, *args: Any) -> Any: ...


#: Events dispatched with ``ctx.serial`` (first non-None result is the answer).
SHORT_CIRCUIT_EVENTS = frozenset({
    Events.BEFORE_USER_MESSAGE_ACCEPT,
    Events.BEFORE_CONTEXT,
    Events.AFTER_CONTEXT,
    Events.BEFORE_MODEL_REQUEST,
    Events.BEFORE_AGENT,
    Events.BEFORE_TOOL_SCHEMA_BIND,
    Events.AFTER_AGENT,
    Events.BEFORE_TOOLS,
    Events.BEFORE_TOOL_CALL,
    Events.AFTER_TOOLS,
})


@dataclass
class EventContext:
    """Payload object passed to runtime event listeners.

    Plugin listeners capture their declared services when they register;
    event payloads never expose the application service container.
    """

    messages: list[Message] = field(default_factory=list)
    settings: LoopSettings | None = None
    continuation: bool = False
    session: SessionInfo | None = None
    user_input: str | None = None
    turn_complete: bool = False
    context_messages: list[Message] | None = None
    agent_response: ModelResponse | None = None
    model_request: ModelRequest | None = None
    model_response: ModelResponse | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call: ToolCall | None = None
    args: dict[str, Any] | None = None
    tool_result: Message | None = None
    tool_results: list[Message] | None = None
    error: BaseException | None = None
    rebuild: bool = False
    client_event: ClientEvent | None = None
    stop_reason: str | None = None
    request_id: str = ""


__all__ = [
    "EventContext",
    "EventPort",
    "Events",
    "SHORT_CIRCUIT_EVENTS",
]
