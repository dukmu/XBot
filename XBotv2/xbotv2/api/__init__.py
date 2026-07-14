"""Supported extension API for XBotv2.

Applications and plugins should import from this package. Modules outside this
package are implementation details and may change without a compatibility shim.
"""

from xbotv2.api.context import ContextComponent, PromptFragmentStage
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
from xbotv2.api.paths import RuntimePaths, SessionPaths
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
from xbotv2.api.messages import Message, ModelChunk, ModelResponse

__all__ = [
    "ArtifactRef",
    "ClientEvent",
    "Command",
    "CommandResult",
    "ContextComponent",
    "HookAction",
    "HookContext",
    "HookDecision",
    "HookStage",
    "JsonValue",
    "Message",
    "ModelChunk",
    "ModelResponse",
    "PluginBase",
    "PluginConfigError",
    "PluginSetupContext",
    "PluginManifest",
    "PluginStore",
    "PromptFragmentStage",
    "RuntimePluginContext",
    "RuntimePaths",
    "SessionInfo",
    "SessionPaths",
    "ToolCall",
    "ToolCallDelta",
    "ToolError",
    "ToolResult",
    "Tool",
    "ToolRegistrationOptions",
]
