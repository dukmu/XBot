"""Hook stage definitions and HookContext dataclass."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from xbotv2.plugin.store import PluginStore


class HookStage(enum.Enum):
    """All hook lifecycle stages in XBotv2.

    Loop hooks and pre-request guard hooks short-circuit on truthy return.
    All other hooks run all registered callbacks regardless of return value.
    """

    # -- Session lifecycle ------------------------------------------------
    ON_SESSION_INIT = "on_session_init"
    ON_SESSION_START = "on_session_start"
    ON_SESSION_RESUME = "on_session_resume"
    ON_SESSION_CLOSE = "on_session_close"

    # -- Turn lifecycle ---------------------------------------------------
    ON_TURN_START = "on_turn_start"
    ON_TURN_END = "on_turn_end"

    # -- Loop lifecycle (short-circuit enabled) ---------------------------
    BEFORE_CONTEXT = "before_context"
    AFTER_CONTEXT = "after_context"
    AFTER_CONTEXT_BUILD = "after_context_build"
    BEFORE_AGENT = "before_agent"
    AFTER_TOOL_SCHEMA_BIND = "after_tool_schema_bind"
    BEFORE_MODEL_REQUEST = "before_model_request"
    AFTER_MODEL_RESPONSE = "after_model_response"
    AFTER_AGENT = "after_agent"
    BEFORE_TOOLS = "before_tools"
    AFTER_TOOLS = "after_tools"

    # -- Message lifecycle ------------------------------------------------
    ON_USER_MESSAGE = "on_user_message"
    ON_ASSISTANT_MESSAGE = "on_assistant_message"
    ON_TOOL_MESSAGE = "on_tool_message"

    # -- System events ----------------------------------------------------
    ON_ERROR = "on_error"
    ON_CONFIG_RELOAD = "on_config_reload"


# Stages that permit short-circuit (first truthy return stops execution)
SHORT_CIRCUIT_STAGES = frozenset({
    HookStage.BEFORE_CONTEXT,
    HookStage.AFTER_CONTEXT,
    HookStage.BEFORE_MODEL_REQUEST,
    HookStage.BEFORE_AGENT,
    HookStage.AFTER_AGENT,
    HookStage.BEFORE_TOOLS,
    HookStage.AFTER_TOOLS,
})

# Stages where pluggable tool registration is allowed
TOOL_REGISTRATION_STAGES = frozenset({
    HookStage.ON_SESSION_INIT,
    HookStage.ON_CONFIG_RELOAD,
})


HookFn = Callable[["HookContext"], "Any | None"]


@dataclass
class SessionInfo:
    """Core session metadata — minimal, no DAG/plan/skills fields."""

    session_id: str
    thread_id: str
    personality_id: str
    turn_count: int = 0
    event_count: int = 0
    status: str = "active"  # active | error | interrupted | closed
    mailbox_pending: int = 0


@dataclass
class HookContext:
    """Context passed to every hook callback.

    Loop hooks may set ``short_circuit_result`` to a truthy value to
    short-circuit the stage.
    """

    stage: HookStage
    state: dict[str, Any] = field(default_factory=dict)
    config: Any | None = None  # AgentConfig (avoid circular import)
    tools: Any | None = None  # ToolRegistry
    plugin_store: "PluginStore | None" = None
    session: SessionInfo = field(default_factory=lambda: SessionInfo(
        session_id="", thread_id="", personality_id="",
    ))
    emit: Callable[[Any], None] = field(default=lambda _: None)

    # Stage-specific data (populated by engine before hook execution)
    user_input: str | None = None
    context_messages: list[Any] | None = None
    agent_response: Any | None = None
    model_request: dict[str, Any] | None = None
    model_response: Any | None = None
    tool_results: list[Any] | None = None
    error: Exception | None = None
    short_circuit_result: Any | None = None
