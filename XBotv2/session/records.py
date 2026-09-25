"""Transport-neutral projections of canonical conversation messages.

The application/session boundary exposes these records to history readers and
live event producers.  They are projections only: mutation and model-context
construction continue to use :mod:`XBotv2.core.messages`.
"""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, RootModel

from XBotv2.core.artifacts import ArtifactRef, ImageRef
from XBotv2.core.messages import (
    AssistantMessage,
    CompactionSummaryMessage,
    ConversationMessage,
    HumanInputMessage,
    RuntimeNoticeMessage,
    ToolMessage,
)
from XBotv2.core.domain import ModelStop, ModelTiming, ToolTiming
from XBotv2.core.parts import ImagePart, ReasoningPart, TextPart
from XBotv2.core.tools import ToolCall, ToolCallRef, ToolOutcome


class HumanInputRecord(BaseModel):
    kind: Literal["human_input"] = "human_input"
    id: str
    content: str
    images: tuple[ImageRef, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeNoticeRecord(BaseModel):
    kind: Literal["runtime_notice"] = "runtime_notice"
    id: str
    source: str
    event: str
    content: str
    images: tuple[ImageRef, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    model_config = ConfigDict(extra="forbid", frozen=True)


class AssistantRecord(BaseModel):
    kind: Literal["assistant"] = "assistant"
    id: str
    content: str
    reasoning: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    timing: ModelTiming
    stop: ModelStop
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolRecord(BaseModel):
    kind: Literal["tool"] = "tool"
    id: str
    call: ToolCallRef
    outcome: ToolOutcome
    timing: ToolTiming
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompactionSummaryRecord(BaseModel):
    kind: Literal["compaction_summary"] = "compaction_summary"
    id: str
    summary: str
    model_config = ConfigDict(extra="forbid", frozen=True)


ConversationRecord: TypeAlias = (
    HumanInputRecord
    | RuntimeNoticeRecord
    | AssistantRecord
    | ToolRecord
    | CompactionSummaryRecord
)


class InputRecordPayload(RootModel[HumanInputRecord | RuntimeNoticeRecord]):
    """Wire envelope for the two canonical input-record variants."""


def _text(parts: tuple[object, ...]) -> str:
    return "".join(part.text for part in parts if isinstance(part, TextPart))


def project_human_input(message: HumanInputMessage) -> HumanInputRecord:
    """Project the canonical human input used by history and live events."""
    images = tuple(part.image for part in message.parts if isinstance(part, ImagePart))
    return HumanInputRecord(
        id=str(message.id),
        content=_text(message.parts),
        images=images,
        artifacts=message.artifacts,
    )


def project_message(message: ConversationMessage) -> ConversationRecord:
    """Project one canonical message without creating a second message model."""
    if isinstance(message, HumanInputMessage):
        return project_human_input(message)
    if isinstance(message, RuntimeNoticeMessage):
        images = tuple(part.image for part in message.parts if isinstance(part, ImagePart))
        return RuntimeNoticeRecord(
            id=str(message.id), source=message.source, event=message.event,
            content=_text(message.parts), images=images, artifacts=message.artifacts,
        )
    if isinstance(message, AssistantMessage):
        return AssistantRecord(
            id=str(message.id),
            content=_text(message.parts),
            reasoning="".join(part.text for part in message.parts if isinstance(part, ReasoningPart)),
            tool_calls=tuple(part for part in message.parts if isinstance(part, ToolCall)),
            timing=message.exchange.timing,
            stop=message.exchange.stop,
        )
    if isinstance(message, ToolMessage):
        return ToolRecord(
            id=str(message.id), call=message.call, outcome=message.outcome,
            timing=message.timing,
        )
    if isinstance(message, CompactionSummaryMessage):
        return CompactionSummaryRecord(id=str(message.id), summary=message.summary)
    raise TypeError(f"Unsupported conversation message: {type(message).__name__}")


__all__ = [
    "AssistantRecord",
    "CompactionSummaryRecord",
    "ConversationRecord",
    "HumanInputRecord",
    "InputRecordPayload",
    "RuntimeNoticeRecord",
    "ToolRecord",
    "project_human_input",
    "project_message",
]
