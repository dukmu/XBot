"""Shared contracts for XBotv2.

Plugins and applications import the stable contracts from this package
(``XBotv2.core``).  Event names live in ``XBotv2.core.events``; the job-system
contract lives in ``XBotv2.jobs``; engine internals are implementation
details and may change without a compatibility shim.
"""

from XBotv2.core.agents import (
    AgentDefinition,
    AgentMode,
    AgentRuntime,
    AgentSession,
    AgentSessionResult,
    ChildEngineFactory,
    SubagentAgentError,
    SubagentTurnError,
)
from XBotv2.core.commands import Command, CommandResult
from XBotv2.core.context import ContextComponent, PromptFragmentStage
from XBotv2.core.events import (
    EventContext,
    Events,
    SHORT_CIRCUIT_EVENTS,
    ToolAction,
    ToolDecision,
)
from XBotv2.core.messages import (
    ContentPart,
    ImageContent,
    ImagePart,
    Message,
    ModelChunk,
    ModelResponse,
    ReasoningPart,
    TextPart,
    ToolCallPart,
)
from XBotv2.core.paths import RuntimePaths, SessionPaths, ThreadPaths
from XBotv2.core.prompts import MESSAGE_FORMAT_KEY, prompt_container, prompt_element
from XBotv2.core.providers import InputModality, ProviderCapabilities
from XBotv2.core.runtime import SessionInfo
from XBotv2.core.tokens import (
    calibrated_context_tokens,
    context_token_limit,
    estimate_messages_tokens,
    estimate_request_tokens,
)
from XBotv2.core.tools import (
    ArtifactRef,
    ClientEvent,
    JsonValue,
    Tool,
    ToolCall,
    ToolCallDelta,
    ToolError,
    ToolResult,
)
from XBotv2.core.variables import RuntimeVariables

__all__ = [
    "AgentDefinition",
    "AgentMode",
    "AgentRuntime",
    "AgentSession",
    "AgentSessionResult",
    "ArtifactRef",
    "ChildEngineFactory",
    "ClientEvent",
    "Command",
    "CommandResult",
    "ContentPart",
    "ContextComponent",
    "EventContext",
    "Events",
    "ImageContent",
    "ImagePart",
    "InputModality",
    "JsonValue",
    "MESSAGE_FORMAT_KEY",
    "Message",
    "ModelChunk",
    "ModelResponse",
    "ProviderCapabilities",
    "PromptFragmentStage",
    "ReasoningPart",
    "RuntimePaths",
    "RuntimeVariables",
    "SessionInfo",
    "SessionPaths",
    "SHORT_CIRCUIT_EVENTS",
    "SubagentAgentError",
    "SubagentTurnError",
    "TextPart",
    "ThreadPaths",
    "Tool",
    "ToolAction",
    "ToolCall",
    "ToolCallDelta",
    "ToolCallPart",
    "ToolDecision",
    "ToolError",
    "ToolResult",
    "calibrated_context_tokens",
    "context_token_limit",
    "estimate_messages_tokens",
    "estimate_request_tokens",
    "prompt_container",
    "prompt_element",
]
