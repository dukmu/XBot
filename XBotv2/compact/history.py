"""History-boundary and accounting helpers for conversation compaction."""

from __future__ import annotations

from typing import Sequence

from XBotv2.core import Message


def history_chars(messages: Sequence[Message]) -> int:
    total = 0
    for message in messages:
        total += len(str(message.content or ""))
        for call in message.tool_calls or []:
            total += len(call.name) + len(str(call.args))
    return total


def compact_prefix_end(messages: Sequence[Message], keep_recent_turns: int) -> int:
    """Return a safe prefix end while preserving recent logical boundaries.

    Human-user boundaries take precedence.  Goal/continuation histories that
    contain too few user messages fall back to assistant iteration boundaries;
    splitting at an assistant keeps any following ToolResult messages paired
    with the retained ToolUse.
    """
    if keep_recent_turns < 1:
        raise ValueError("keep_recent_turns must be >= 1")

    user_indexes = [
        index
        for index, message in enumerate(messages)
        if message.role == "user"
    ]
    if len(user_indexes) > keep_recent_turns:
        return user_indexes[-keep_recent_turns]

    assistant_indexes = [
        index
        for index, message in enumerate(messages)
        if message.role == "assistant"
    ]
    if len(assistant_indexes) > keep_recent_turns:
        return assistant_indexes[-keep_recent_turns]
    return 0


def leading_system_messages(messages: Sequence[Message]) -> list[Message]:
    """Return the stable leading system prefix of a provider request."""
    prefix: list[Message] = []
    for message in messages:
        if message.role != "system":
            break
        prefix.append(message)
    return prefix


__all__ = [
    "compact_prefix_end",
    "history_chars",
    "leading_system_messages",
]
