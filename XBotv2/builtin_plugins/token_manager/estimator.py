"""Provider-neutral character-based token approximation."""

from __future__ import annotations

from typing import Any

_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def estimate_message_tokens(message: Any) -> int:
    content = getattr(message, "content", "") or ""
    total = estimate_tokens(content)
    for tc in getattr(message, "tool_calls", []) or []:
        total += estimate_tokens(tc.name)
        total += estimate_tokens(str(tc.args))
    return total


def estimate_context_tokens(context_messages: list[Any]) -> int:
    return sum(estimate_message_tokens(m) for m in context_messages)


def estimate_tool_schema_tokens(tools: list[Any]) -> int:
    total = 0
    for tool in tools:
        schema = getattr(tool, "provider_schema", None)
        if schema:
            total += estimate_tokens(str(schema()))
        elif hasattr(tool, "function"):
            total += estimate_tokens(str(getattr(tool.function, "name", "")))
            total += estimate_tokens(str(getattr(tool.function, "description", "")))
    return total
