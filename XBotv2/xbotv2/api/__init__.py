"""Supported extension API for XBotv2.

Applications and plugins should import from this package. Modules outside this
package are implementation details and may change without a compatibility shim.
"""

from xbotv2.api.plugins import (
    PluginBase,
    PluginManifest,
    PluginSetupContext,
    PluginStore,
)
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
from xbotv2.api.hooks import HookAction, HookContext, HookDecision, HookStage
from xbotv2.api.messages import Message, ModelChunk, ModelResponse

__all__ = [
    "ArtifactRef",
    "ClientEvent",
    "HookAction",
    "HookContext",
    "HookDecision",
    "HookStage",
    "JsonValue",
    "Message",
    "ModelChunk",
    "ModelResponse",
    "PluginBase",
    "PluginSetupContext",
    "PluginManifest",
    "PluginStore",
    "SessionInfo",
    "ToolCall",
    "ToolCallDelta",
    "ToolError",
    "ToolResult",
    "Tool",
]
