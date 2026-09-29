"""Session-owned events published by one live runtime."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from XBotv2.core.domain import MessageId, ResolvedRuntimeSelection
from XBotv2.session.contracts import HistoryMutation, PendingInputData
from XBotv2.session.records import InputRecordPayload


class _SessionEventModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentConfiguredEvent(_SessionEventModel):
    kind: Literal["agent_configured"] = "agent_configured"
    runtime_selection: ResolvedRuntimeSelection


class HistoryUpdatedEvent(_SessionEventModel):
    kind: Literal["history_updated"] = "history_updated"
    operation: str = Field(min_length=1)
    mutation: HistoryMutation


class MessagePublishedEvent(_SessionEventModel):
    kind: Literal["message"] = "message"
    record: InputRecordPayload


class QueueReplacedEvent(_SessionEventModel):
    kind: Literal["queue_updated"] = "queue_updated"
    items: tuple[PendingInputData, ...] = ()


class InputAcceptedEvent(_SessionEventModel):
    kind: Literal["input_accepted"] = "input_accepted"
    message_ids: tuple[MessageId, ...] = Field(min_length=1)
    target: Literal["next-turn", "next-step"]


class InputClaimedEvent(_SessionEventModel):
    kind: Literal["input_claimed"] = "input_claimed"
    message_ids: tuple[MessageId, ...] = Field(min_length=1)


class InputConsumedEvent(_SessionEventModel):
    kind: Literal["input_consumed"] = "input_consumed"
    message_ids: tuple[MessageId, ...] = Field(min_length=1)


__all__ = [
    "AgentConfiguredEvent",
    "HistoryUpdatedEvent",
    "InputAcceptedEvent",
    "InputClaimedEvent",
    "InputConsumedEvent",
    "MessagePublishedEvent",
    "QueueReplacedEvent",
]
