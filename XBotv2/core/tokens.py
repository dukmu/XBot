"""Provider-neutral context estimation shared by runtime plugins."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from XBotv2.core.messages import Message
from XBotv2.core.tools import Tool, provider_tool_schema

REQUEST_ESTIMATE_KEY = "xbotv2_request_estimated_tokens"
REQUEST_CONTEXT_WINDOW_KEY = "xbotv2_request_context_window"
REQUEST_PROVIDER_KEY = "xbotv2_request_provider"
REQUEST_MODEL_KEY = "xbotv2_request_model"
REQUEST_CONTEXT_TOKENS_KEY = "xbotv2_request_context_tokens"

_ANCHOR_FIELDS: tuple[tuple[str, str], ...] = (
    (REQUEST_PROVIDER_KEY, "provider"),
    (REQUEST_MODEL_KEY, "model"),
    (REQUEST_CONTEXT_WINDOW_KEY, "context_window"),
    (REQUEST_ESTIMATE_KEY, "request_estimate"),
    (REQUEST_CONTEXT_TOKENS_KEY, "context_tokens"),
)


class RequestAnchor(BaseModel):
    """The request facts stored with one message for later calibration.

    The provider's exact context size is only reusable by a request with the
    same route, so provider, model, and window travel with the measurement
    instead of being re-derived by every reader.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = ""
    model: str = ""
    context_window: int = Field(default=0, ge=0)
    request_estimate: int = Field(default=0, ge=0)
    context_tokens: int = Field(default=0, ge=0)

    def matches(
        self,
        *,
        provider: str = "",
        model: str = "",
        context_window: int = 0,
    ) -> bool:
        """Whether this anchor describes the same provider route."""
        if provider and self.provider != provider:
            return False
        if model and self.model != model:
            return False
        if context_window and self.context_window != context_window:
            return False
        return True


def write_request_anchor(message: Message, anchor: RequestAnchor) -> None:
    """Record one anchor on a message; the sole writer of the anchor keys."""
    metadata = message.response_metadata
    metadata[REQUEST_PROVIDER_KEY] = anchor.provider
    metadata[REQUEST_MODEL_KEY] = anchor.model
    metadata[REQUEST_CONTEXT_WINDOW_KEY] = anchor.context_window
    metadata[REQUEST_ESTIMATE_KEY] = anchor.request_estimate
    metadata[REQUEST_CONTEXT_TOKENS_KEY] = anchor.context_tokens


def read_request_anchor(message: Message) -> RequestAnchor | None:
    """Read a stored anchor, or ``None`` when the message carries none."""
    metadata = message.response_metadata
    if not any(key in metadata for key, _ in _ANCHOR_FIELDS):
        return None
    return RequestAnchor(**{
        field: metadata[key]
        for key, field in _ANCHOR_FIELDS
        if metadata.get(key) is not None
    })


def estimate_text_tokens(text: str) -> int:
    """Return a conservative estimate for mixed natural language and code."""
    if not text:
        return 0
    return max(
        1,
        (len(text) + 3) // 4,
        (len(text.encode("utf-8")) + 2) // 3,
    )


def estimate_messages_tokens(messages: Sequence[Message]) -> int:
    total = 0
    for message in messages:
        total += 4
        total += estimate_text_tokens(message.role)
        total += estimate_text_tokens(message.content)
        for call in message.tool_calls:
            total += estimate_text_tokens(call.name)
            total += estimate_text_tokens(_stable_json(call.args))
        total += estimate_text_tokens(message.reasoning)
    return total


def estimate_tool_schema_tokens(tools: list[Tool]) -> int:
    return sum(
        estimate_text_tokens(_stable_json(provider_tool_schema(tool)))
        for tool in tools
    )


def estimate_request_tokens(
    messages: Sequence[Message],
    tools: list[Tool] | None = None,
) -> int:
    return estimate_messages_tokens(messages) + estimate_tool_schema_tokens(
        tools or []
    )


def calibrated_context_tokens(
    messages: Sequence[Message],
    tools: list[Tool],
    history: Sequence[Message],
    *,
    provider: str = "",
    model: str = "",
    context_window: int = 0,
) -> tuple[int, int, str]:
    """Estimate the next request using the latest provider measurement.

    The stable request prefix cancels out: only the provider-neutral estimate
    difference between the previous and current request is applied to the last
    exact provider context size.
    """
    current_estimate = estimate_request_tokens(messages, tools)
    for message in reversed(history):
        anchor = read_request_anchor(message)
        if anchor is None:
            continue
        if not anchor.matches(
            provider=provider,
            model=model,
            context_window=context_window,
        ):
            continue
        measured = int(message.usage_metadata.get("context_tokens") or 0)
        context_tokens = measured or anchor.context_tokens
        if context_tokens > 0 and anchor.request_estimate > 0:
            calibrated = max(
                1,
                context_tokens + current_estimate - anchor.request_estimate,
            )
            return calibrated, current_estimate, "provider_calibrated"
    return current_estimate, current_estimate, "estimated"


def context_token_limit(
    max_context_tokens: int,
    *,
    trigger_ratio: float,
    output_reservation: int = 0,
) -> int:
    """Return the input threshold after ratio and output safety constraints."""
    ratio_limit = int(max_context_tokens * trigger_ratio)
    output_limit = max(1, max_context_tokens - output_reservation)
    return max(1, min(ratio_limit, output_limit))


def _stable_json(value: JsonValue) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "REQUEST_ESTIMATE_KEY",
    "REQUEST_CONTEXT_WINDOW_KEY",
    "REQUEST_CONTEXT_TOKENS_KEY",
    "REQUEST_MODEL_KEY",
    "REQUEST_PROVIDER_KEY",
    "RequestAnchor",
    "calibrated_context_tokens",
    "context_token_limit",
    "estimate_messages_tokens",
    "estimate_request_tokens",
    "estimate_text_tokens",
    "estimate_tool_schema_tokens",
    "read_request_anchor",
    "write_request_anchor",
]
