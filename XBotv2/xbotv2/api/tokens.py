"""Provider-neutral context estimation shared by runtime plugins."""

from __future__ import annotations

import json
from typing import Any

from xbotv2.api.tools import provider_tool_schema

REQUEST_ESTIMATE_KEY = "xbotv2_request_estimated_tokens"
REQUEST_CONTEXT_WINDOW_KEY = "xbotv2_request_context_window"
REQUEST_PROVIDER_KEY = "xbotv2_request_provider"


def estimate_text_tokens(text: str) -> int:
    """Return a conservative estimate for mixed natural language and code."""
    if not text:
        return 0
    return max(
        1,
        (len(text) + 3) // 4,
        (len(text.encode("utf-8")) + 2) // 3,
    )


def estimate_messages_tokens(messages: list[Any]) -> int:
    total = 0
    for message in messages:
        total += 4
        total += estimate_text_tokens(str(getattr(message, "role", "") or ""))
        total += estimate_text_tokens(str(getattr(message, "content", "") or ""))
        for call in getattr(message, "tool_calls", None) or []:
            total += estimate_text_tokens(str(getattr(call, "name", "") or ""))
            total += estimate_text_tokens(_stable_json(getattr(call, "args", {})))
        reasoning = getattr(message, "reasoning", "")
        if isinstance(reasoning, str):
            total += estimate_text_tokens(reasoning)
    return total


def estimate_tool_schema_tokens(tools: list[Any]) -> int:
    return sum(
        estimate_text_tokens(_stable_json(provider_tool_schema(tool)))
        for tool in tools
    )


def estimate_request_tokens(
    messages: list[Any],
    tools: list[Any] | None = None,
) -> int:
    return estimate_messages_tokens(messages) + estimate_tool_schema_tokens(
        tools or []
    )


def calibrated_context_tokens(
    messages: list[Any],
    tools: list[Any],
    history: list[Any],
    *,
    provider: str = "",
    context_window: int = 0,
) -> tuple[int, int, str]:
    """Estimate the next request using the latest provider measurement.

    The stable request prefix cancels out: only the provider-neutral estimate
    difference between the previous and current request is applied to the last
    exact provider context size.
    """
    current_estimate = estimate_request_tokens(messages, tools)
    for message in reversed(history):
        usage = getattr(message, "usage_metadata", {}) or {}
        metadata = getattr(message, "response_metadata", {}) or {}
        context_tokens = int(usage.get("context_tokens") or 0)
        previous_estimate = int(metadata.get(REQUEST_ESTIMATE_KEY) or 0)
        previous_provider = str(metadata.get(REQUEST_PROVIDER_KEY) or "")
        previous_window = int(
            metadata.get(REQUEST_CONTEXT_WINDOW_KEY) or 0
        )
        if provider and previous_provider != provider:
            continue
        if context_window and previous_window != context_window:
            continue
        if context_tokens > 0 and previous_estimate > 0:
            calibrated = max(
                1,
                context_tokens + current_estimate - previous_estimate,
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


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        return str(value)


__all__ = [
    "REQUEST_ESTIMATE_KEY",
    "REQUEST_CONTEXT_WINDOW_KEY",
    "REQUEST_PROVIDER_KEY",
    "calibrated_context_tokens",
    "context_token_limit",
    "estimate_messages_tokens",
    "estimate_request_tokens",
    "estimate_text_tokens",
    "estimate_tool_schema_tokens",
]
