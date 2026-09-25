"""History boundaries for conversation compaction."""

from __future__ import annotations

from typing import Sequence

from XBotv2.core.messages import AssistantMessage, ConversationMessage, HumanInputMessage, ToolMessage
from XBotv2.core.parts import TextPart
from XBotv2.core.tools import ToolCall, ToolFailed, ToolSucceeded


def history_chars(messages: Sequence[ConversationMessage]) -> int:
    total = 0
    for message in messages:
        if isinstance(message, (HumanInputMessage, AssistantMessage)):
            total += sum(len(part.text) for part in message.parts if isinstance(part, TextPart))
        if isinstance(message, ToolMessage):
            if isinstance(message.outcome, (ToolSucceeded, ToolFailed)):
                total += sum(
                    len(part.text)
                    for part in message.outcome.output.parts
                    if isinstance(part, TextPart)
                )
    return total


def compact_prefix_end(messages: Sequence[ConversationMessage], keep_recent_turns: int) -> int:
    if keep_recent_turns < 1:
        raise ValueError("keep_recent_turns must be >= 1")
    user_indexes = [i for i, m in enumerate(messages) if isinstance(m, HumanInputMessage)]
    if len(user_indexes) > keep_recent_turns:
        candidate = user_indexes[-keep_recent_turns]
    else:
        assistant_indexes = [i for i, m in enumerate(messages) if isinstance(m, AssistantMessage)]
        if len(assistant_indexes) <= keep_recent_turns:
            return 0
        candidate = assistant_indexes[-keep_recent_turns]
    boundaries = tool_pairing_boundaries(messages)
    while candidate > 0 and not boundaries[candidate]:
        candidate -= 1
    return candidate


def leading_system_messages(messages: Sequence[ConversationMessage]) -> list[ConversationMessage]:
    return []


def tool_pairing_boundaries(messages: Sequence[ConversationMessage]) -> list[bool]:
    pending: set[str] = set()
    boundaries = [True]
    for message in messages:
        if isinstance(message, AssistantMessage):
            calls = {str(part.id) for part in message.parts if isinstance(part, ToolCall)}
            if calls & pending:
                raise ValueError("duplicate tool call id in conversation history")
            pending.update(calls)
        elif isinstance(message, ToolMessage):
            call_id = str(message.call.id)
            if call_id not in pending:
                raise ValueError("tool result has no matching assistant tool call in history")
            pending.remove(call_id)
        boundaries.append(not pending)
    return boundaries


__all__ = ["compact_prefix_end", "history_chars", "leading_system_messages", "tool_pairing_boundaries"]
