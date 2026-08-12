"""Agent inbox: model-visible runtime notifications.

Internal events (background-job / subagent completions) must not start turns
on their own and must not be forced into the conversation while the agent is
busy. They land here with ``trigger_turn=False`` and are drained — all at
once — when the next turn assembles its model context. Only genuinely
actionable events (approval required, fatal runtime error) set
``trigger_turn=True`` so the session knows it should wake a turn.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InboxMessage:
    """One model-visible runtime notification.

    ``payload`` is small on purpose: the LLM is told a job finished, not given
    its full output. The full result lives in the job registry / output store
    and is read on demand.
    """

    type: str
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    trigger_turn: bool = False


class AgentInbox:
    """A short-lived queue of pending runtime notifications."""

    def __init__(self) -> None:
        self._pending: deque[InboxMessage] = deque()

    def enqueue(self, message: InboxMessage) -> None:
        self._pending.append(message)

    def drain(self) -> list[InboxMessage]:
        """Take every pending message at once."""
        items = list(self._pending)
        self._pending.clear()
        return items

    def __len__(self) -> int:
        return len(self._pending)

    @property
    def pending(self) -> list[InboxMessage]:
        return list(self._pending)
