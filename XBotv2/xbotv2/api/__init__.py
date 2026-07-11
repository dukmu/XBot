"""Supported extension API for XBotv2.

Applications and plugins should import from this package. Modules outside this
package are implementation details and may change without a compatibility shim.
"""

from xbotv2.api.protocol import (
    PROTOCOL_VERSION,
    CommandRequest,
    CommandInfo,
    CommandListResponse,
    CommandResponse,
    CommandResult,
    ErrorResponse,
    Event,
    HelloRequest,
    HelloResponse,
    InteractionResponseRequest,
    MessageRequest,
    OpenSessionRequest,
    OpenSessionResponse,
    ProtocolFrame,
)
from xbotv2.api.types import (
    ArtifactRef,
    ClientEvent,
    HookAction,
    HookDecision,
    JsonValue,
    ToolCall,
    ToolError,
    ToolResult,
)
from xbotv2.core.state import SessionInfo
from xbotv2.hooks.manager import HookManager
from xbotv2.hooks.types import HookContext, HookStage
from xbotv2.llm.messages import Message
from xbotv2.plugin.base import PluginBase, PluginSetupContext
from xbotv2.plugin.manifest import PluginManifest
from xbotv2.plugin.store import PluginStore
from xbotv2.tools.registry import ToolRegistry
from xbotv2.tools.types import XBotTool

__all__ = [
    "ArtifactRef",
    "ClientEvent",
    "CommandRequest",
    "CommandInfo",
    "CommandListResponse",
    "CommandResponse",
    "CommandResult",
    "ErrorResponse",
    "Event",
    "HelloRequest",
    "HelloResponse",
    "HookAction",
    "HookContext",
    "HookDecision",
    "HookManager",
    "HookStage",
    "InteractionResponseRequest",
    "JsonValue",
    "MessageRequest",
    "Message",
    "OpenSessionRequest",
    "OpenSessionResponse",
    "PROTOCOL_VERSION",
    "PluginBase",
    "PluginSetupContext",
    "PluginManifest",
    "PluginStore",
    "ProtocolFrame",
    "SessionInfo",
    "ToolCall",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "XBotTool",
]
