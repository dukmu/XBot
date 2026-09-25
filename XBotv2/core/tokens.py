"""Token estimation and budget values owned by core."""

from __future__ import annotations

import json
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from XBotv2.core.messages import (
    AssistantMessage,
    CompactionSummaryMessage,
    ConversationMessage,
    HumanInputMessage,
    RuntimeNoticeMessage,
    ToolMessage,
)
from XBotv2.core.parts import ReasoningPart, TextPart
from XBotv2.core.provider import (
    ModelRequest,
    ProviderAssistant,
    ProviderSystem,
    ProviderTool,
    ProviderUser,
    ToolSchema,
)
from XBotv2.core.tools import Tool, ToolCall, ToolFailed, ToolSucceeded


class TokenBudget(BaseModel):
    context_window: int = Field(ge=1)
    output_reservation: int = Field(ge=0)
    used: int = Field(ge=0)
    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def remaining(self) -> int:
        return self.context_window - self.output_reservation - self.used

    @property
    def over_budget(self) -> bool:
        return self.remaining < 0

    @property
    def excess(self) -> int:
        return max(0, -self.remaining)


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4, (len(text.encode("utf-8")) + 2) // 3)


def _parts_tokens(parts: Sequence[object]) -> int:
    total = 0
    for part in parts:
        if isinstance(part, (TextPart, ReasoningPart)):
            total += estimate_text_tokens(part.text)
        elif isinstance(part, ToolCall):
            total += estimate_text_tokens(part.name)
            total += estimate_text_tokens(_stable_json(part.args))
    return total


def estimate_messages_tokens(
    messages: Sequence[ConversationMessage | object],
) -> int:
    total = 0
    for message in messages:
        total += 4
        if isinstance(message, (HumanInputMessage, RuntimeNoticeMessage, AssistantMessage)):
            total += _parts_tokens(message.parts)
        elif isinstance(message, ToolMessage):
            total += estimate_text_tokens(message.call.name)
            if isinstance(message.outcome, (ToolSucceeded, ToolFailed)):
                total += _parts_tokens(message.outcome.output.parts)
        elif isinstance(message, CompactionSummaryMessage):
            total += estimate_text_tokens(message.summary)
        elif isinstance(
            message,
            (ProviderSystem, ProviderUser, ProviderAssistant, ProviderTool),
        ):
            total += _parts_tokens(message.parts)
            if isinstance(message, ProviderTool):
                total += estimate_text_tokens(message.call_id)
        else:
            raise TypeError(f"Unsupported message for token estimation: {message!r}")
    return total


def estimate_tool_schema_tokens(tools: Sequence[Tool | ToolSchema]) -> int:
    return sum(estimate_text_tokens(_stable_json(
        tool.provider_schema() if isinstance(tool, Tool) else tool.model_dump(mode="json")
    )) for tool in tools)


def estimate_request_tokens(
    messages: Sequence[ConversationMessage | object],
    tools: Sequence[Tool | ToolSchema] = (),
) -> int:
    return estimate_messages_tokens(messages) + estimate_tool_schema_tokens(tools)


def estimate_model_request_tokens(request: ModelRequest) -> int:
    return estimate_request_tokens(request.messages, request.tools)


def context_token_limit(
    max_context_tokens: int,
    *,
    trigger_ratio: float,
    output_reservation: int = 0,
) -> int:
    ratio_limit = int(max_context_tokens * trigger_ratio)
    output_limit = max(1, max_context_tokens - output_reservation)
    return max(1, min(ratio_limit, output_limit))


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


__all__ = [
    "TokenBudget",
    "context_token_limit",
    "estimate_messages_tokens",
    "estimate_model_request_tokens",
    "estimate_request_tokens",
    "estimate_text_tokens",
    "estimate_tool_schema_tokens",
]
