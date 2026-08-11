"""Supported extension API for XBotv2.

Applications and plugins should import from this package. Modules outside this
package are implementation details and may change without a compatibility shim.
"""

from xbotv2.api.context import ContextComponent, PromptFragmentStage
from xbotv2.api.agents import AgentDefinition, AgentMode, AgentRuntime
from xbotv2.api.commands import Command, CommandResult
from xbotv2.api.plugins import (
    PluginBase,
    PluginConfigError,
    PluginManifest,
    PluginSetupContext,
    PluginStore,
    RuntimePluginContext,
    ToolRegistrationOptions,
)
from xbotv2.api.paths import RuntimePaths, SessionPaths, ThreadPaths
from xbotv2.api.variables import RuntimeVariables
from xbotv2.api.tools import (
    ArtifactRef,
    ClientEvent,
    JsonValue,
    Tool,
    ToolCall,
    ToolCallDelta,
    ToolError,
    ToolResult,
)
from xbotv2.api.runtime import SessionInfo
from xbotv2.api.hooks import (
    HookAction,
    HookContext,
    HookDecision,
    HookStage,
)
from xbotv2.api.messages import (
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
from xbotv2.api.providers import InputModality, ProviderCapabilities
from xbotv2.api.prompts import MESSAGE_FORMAT_KEY, prompt_container, prompt_element
from xbotv2.api.tokens import (
    calibrated_context_tokens,
    context_token_limit,
    estimate_messages_tokens,
    estimate_request_tokens,
)

__all__ = [
    "ArtifactRef",
    "AgentDefinition",
    "AgentMode",
    "AgentRuntime",
    "ClientEvent",
    "Command",
    "CommandResult",
    "ContentPart",
    "ContextComponent",
    "HookAction",
    "HookContext",
    "HookDecision",
    "HookStage",
    "JsonValue",
    "ImageContent",
    "ImagePart",
    "InputModality",
    "Message",
    "MESSAGE_FORMAT_KEY",
    "ModelChunk",
    "ModelResponse",
    "PluginBase",
    "PluginConfigError",
    "PluginSetupContext",
    "PluginManifest",
    "PluginStore",
    "ProviderCapabilities",
    "ReasoningPart",
    "PromptFragmentStage",
    "calibrated_context_tokens",
    "context_token_limit",
    "estimate_messages_tokens",
    "estimate_request_tokens",
    "prompt_container",
    "prompt_element",
    "RuntimePluginContext",
    "RuntimePaths",
    "RuntimeVariables",
    "SessionInfo",
    "SessionPaths",
    "ThreadPaths",
    "TextPart",
    "ToolCall",
    "ToolCallDelta",
    "ToolCallPart",
    "ToolError",
    "ToolResult",
    "Tool",
    "ToolRegistrationOptions",
]
