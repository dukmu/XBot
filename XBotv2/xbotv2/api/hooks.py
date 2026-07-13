"""Stable hook lifecycle and control-flow types."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from xbotv2.api.context import ContextComponent
from xbotv2.api.messages import Message, ModelResponse
from xbotv2.api.runtime import SessionInfo
from xbotv2.api.tools import ToolCall


class HookStage(enum.Enum):
    ON_SESSION_INIT = "on_session_init"
    ON_SESSION_START = "on_session_start"
    ON_SESSION_RESUME = "on_session_resume"
    ON_SESSION_CLOSE = "on_session_close"
    ON_TURN_START = "on_turn_start"
    ON_TURN_END = "on_turn_end"
    ON_STOP = "on_stop"
    ON_STOP_FAILURE = "on_stop_failure"
    BEFORE_USER_MESSAGE_ACCEPT = "before_user_message_accept"
    AFTER_USER_MESSAGE_ACCEPT = "after_user_message_accept"
    BEFORE_CONTEXT = "before_context"
    PRE_COMPACT = "pre_compact"
    POST_COMPACT = "post_compact"
    BEFORE_CONTEXT_BUILD = "before_context_build"
    AFTER_CONTEXT = "after_context"
    AFTER_CONTEXT_COMPONENTS_BUILD = "after_context_components_build"
    AFTER_CONTEXT_BUILD = "after_context_build"
    BEFORE_AGENT = "before_agent"
    BEFORE_TOOL_SCHEMA_BIND = "before_tool_schema_bind"
    AFTER_TOOL_SCHEMA_BIND = "after_tool_schema_bind"
    BEFORE_MODEL_REQUEST = "before_model_request"
    AFTER_MODEL_RESPONSE = "after_model_response"
    ON_MODEL_REQUEST_ERROR = "on_model_request_error"
    AFTER_AGENT = "after_agent"
    BEFORE_TOOLS = "before_tools"
    AFTER_TOOLS = "after_tools"
    ON_USER_MESSAGE = "on_user_message"
    ON_ASSISTANT_MESSAGE = "on_assistant_message"
    ON_TOOL_MESSAGE = "on_tool_message"
    ON_TOOL_CALLS_PARSED = "on_tool_calls_parsed"
    ON_PERMISSION_REQUEST = "on_permission_request"
    ON_PERMISSION_DENIED = "on_permission_denied"
    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_CALL = "after_tool_call"
    ON_TOOL_CALL_FAILURE = "on_tool_call_failure"
    POST_TOOL_BATCH = "post_tool_batch"
    ON_TOOL_DENIED = "on_tool_denied"
    ON_CLIENT_EVENT = "on_client_event"
    BEFORE_STATE_PERSIST = "before_state_persist"
    AFTER_STATE_PERSIST = "after_state_persist"
    ON_ERROR = "on_error"


SHORT_CIRCUIT_STAGES = frozenset({
    HookStage.BEFORE_CONTEXT,
    HookStage.PRE_COMPACT,
    HookStage.BEFORE_CONTEXT_BUILD,
    HookStage.AFTER_CONTEXT,
    HookStage.BEFORE_MODEL_REQUEST,
    HookStage.BEFORE_AGENT,
    HookStage.BEFORE_TOOL_SCHEMA_BIND,
    HookStage.AFTER_AGENT,
    HookStage.BEFORE_TOOLS,
    HookStage.BEFORE_TOOL_CALL,
    HookStage.AFTER_TOOLS,
})

STRICT_FAILURE_STAGES = frozenset({
    HookStage.ON_SESSION_INIT,
    HookStage.ON_SESSION_CLOSE,
    HookStage.BEFORE_STATE_PERSIST,
    HookStage.AFTER_STATE_PERSIST,
    HookStage.ON_STOP,
})


class HookAction(str, enum.Enum):
    CONTINUE = "continue"
    DENY = "deny"
    STOP = "stop"


@dataclass(frozen=True)
class HookDecision:
    action: HookAction = HookAction.CONTINUE
    reason: str = ""
    value: Any = None


@dataclass
class HookContext:
    stage: HookStage
    state: dict[str, Any] = field(default_factory=dict)
    config: Any | None = None
    tools: Any | None = None
    sandbox: Any | None = None
    plugin_store: Any | None = None
    plugin_runtime: Any | None = None
    invoke_model: Callable[[list[Message]], Awaitable[ModelResponse]] | None = None
    request_user_input: Callable[..., Awaitable[dict[str, Any]]] | None = None
    session: SessionInfo = field(default_factory=lambda: SessionInfo("", ""))
    emit: Callable[[Any], None] = field(default=lambda _: None)
    user_input: str | None = None
    context_components: list[ContextComponent] | None = None
    context_messages: list[Any] | None = None
    agent_response: Any | None = None
    model_request: dict[str, Any] | None = None
    model_response: Any | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call: ToolCall | None = None
    tool_results: list[Any] | None = None
    tool_result: Any | None = None
    stop_reason: str | None = None
    compact_reason: str | None = None
    permission_decision: str | None = None
    client_event: dict[str, Any] | None = None
    error: Exception | None = None
    short_circuit_result: Any | None = None
    request_id: str = ""

HookFn = Callable[[HookContext], Any]

__all__ = [
    "HookAction",
    "HookContext",
    "HookDecision",
    "HookFn",
    "HookStage",
    "SHORT_CIRCUIT_STAGES",
    "STRICT_FAILURE_STAGES",
]
