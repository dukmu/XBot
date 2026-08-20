"""Shared contracts for XBotv2.

Plugins and applications import capability-neutral contracts from this
package. Plugin-owned declarations are exported by their owning package roots;
engine internals remain implementation details.
"""

from XBotv2.core.context import ContextComponent, PromptFragmentStage
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
from XBotv2.core.operations import (
    EmptyRequest,
    Operation,
    OperationContext,
    dispatch_operation,
)
from XBotv2.core.paths import RuntimePaths, SessionPaths, ThreadPaths
from XBotv2.core.prompts import MESSAGE_FORMAT_KEY, prompt_container, prompt_element
from XBotv2.core.providers import (
    BaseProvider,
    InputModality,
    ProviderCapabilities,
    ProviderRetryExhaustedError,
)
from XBotv2.core.tokens import (
    calibrated_context_tokens,
    context_token_limit,
    estimate_messages_tokens,
    estimate_request_tokens,
)
from XBotv2.core.tools import (
    ArtifactRef,
    ClientEvent,
    JsonObject,
    JsonValue,
    Tool,
    ToolCall,
    ToolCallDelta,
    ToolError,
    ToolResult,
)
from XBotv2.core.variables import RuntimeVariables

__all__ = [
    "ArtifactRef",
    "ClientEvent",
    "ContentPart",
    "ContextComponent",
    "EmptyRequest",
    "Operation",
    "OperationContext",
    "ImageContent",
    "ImagePart",
    "InputModality",
    "JsonObject",
    "JsonValue",
    "MESSAGE_FORMAT_KEY",
    "Message",
    "ModelChunk",
    "ModelResponse",
    "BaseProvider",
    "ProviderRetryExhaustedError",
    "ProviderCapabilities",
    "PromptFragmentStage",
    "ReasoningPart",
    "RuntimePaths",
    "RuntimeVariables",
    "SessionPaths",
    "TextPart",
    "ThreadPaths",
    "Tool",
    "ToolCall",
    "ToolCallDelta",
    "ToolCallPart",
    "ToolError",
    "ToolResult",
    "calibrated_context_tokens",
    "context_token_limit",
    "estimate_messages_tokens",
    "dispatch_operation",
    "estimate_request_tokens",
    "prompt_container",
    "prompt_element",
]
