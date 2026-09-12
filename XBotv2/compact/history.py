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
        candidate = user_indexes[-keep_recent_turns]
    else:
        assistant_indexes = [
            index
            for index, message in enumerate(messages)
            if message.role == "assistant"
        ]
        if len(assistant_indexes) > keep_recent_turns:
            candidate = assistant_indexes[-keep_recent_turns]
        else:
            return 0

    # Never leave an assistant tool call on the opposite side of the cut.  A
    # malformed result is rejected by the same fold instead of being hidden by
    # compaction.  Build all cut states once, rather than rescanning history
    # while backing up over a tool iteration.
    boundaries = tool_pairing_boundaries(messages)
    while candidate > 0 and not boundaries[candidate]:
        candidate -= 1
    return candidate


def leading_system_messages(messages: Sequence[Message]) -> list[Message]:
    """Return the stable leading system prefix of a provider request."""
    prefix: list[Message] = []
    for message in messages:
        if message.role != "system":
            break
        prefix.append(message)
    return prefix


def _tool_call_ids(message: Message) -> set[str]:
    return {call.id for call in message.tool_calls or () if call.id}


def tool_pairing_boundaries(messages: Sequence[Message]) -> list[bool]:
    """Return tool-pair balance for every cut of the message sequence.

    ``boundaries[i]`` describes the cut before ``messages[i]``; the final entry
    describes the cut after the last message.  A malformed surface raises here
    rather than being silently accepted by a later reader.
    """
    pending: set[str] = set()
    boundaries = [True]
    for index, message in enumerate(messages):
        if message.role == "assistant":
            calls = _tool_call_ids(message)
            if calls & pending:
                raise ValueError("duplicate tool call id in conversation history")
            pending.update(calls)
        elif message.role == "tool":
            call_id = message.tool_call_id
            if not call_id or call_id not in pending:
                raise ValueError(
                    "tool result has no matching assistant tool call in history"
                )
            pending.remove(call_id)
        boundaries.append(not pending)
    return boundaries


__all__ = [
    "compact_prefix_end",
    "history_chars",
    "leading_system_messages",
    "tool_pairing_boundaries",
]
