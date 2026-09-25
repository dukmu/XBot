"""Canonical persisted conversation messages.

Provider roles are produced by the context compiler. These classes model the
conversation itself and contain no transport envelope or provider metadata bag.
"""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict

from XBotv2.core.artifacts import ArtifactRef
from XBotv2.core.domain import InputId, MessageId, ModelExchange, NoticeId, ToolTiming
from XBotv2.core.parts import ImagePart, ReasoningPart, TextPart
from XBotv2.core.tools import ToolCall, ToolCallRef, ToolOutcome

TextContent: TypeAlias = TextPart | ImagePart
AssistantContent: TypeAlias = TextPart | ReasoningPart | ToolCall


class HumanInputMessage(BaseModel):
    kind: Literal["human_input"] = "human_input"
    id: MessageId
    input_id: InputId
    parts: tuple[TextContent, ...]
    artifacts: tuple[ArtifactRef, ...] = ()
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeNoticeMessage(BaseModel):
    kind: Literal["runtime_notice"] = "runtime_notice"
    id: MessageId
    notice_id: NoticeId
    source: str
    event: str
    parts: tuple[TextContent, ...]
    artifacts: tuple[ArtifactRef, ...] = ()
    model_config = ConfigDict(extra="forbid", frozen=True)


class AssistantMessage(BaseModel):
    kind: Literal["assistant"] = "assistant"
    id: MessageId
    parts: tuple[AssistantContent, ...]
    exchange: ModelExchange
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolMessage(BaseModel):
    kind: Literal["tool"] = "tool"
    id: MessageId
    call: ToolCallRef
    outcome: ToolOutcome
    timing: ToolTiming
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompactionSummaryMessage(BaseModel):
    kind: Literal["compaction_summary"] = "compaction_summary"
    id: MessageId
    summary: str
    model_config = ConfigDict(extra="forbid", frozen=True)


ConversationMessage: TypeAlias = (
    HumanInputMessage
    | RuntimeNoticeMessage
    | AssistantMessage
    | ToolMessage
    | CompactionSummaryMessage
)

# Resolve the execution envelope's forward reference after the canonical
# ToolMessage has been declared; the two domain modules otherwise form a
# natural cycle (ToolOutcome belongs to tools, ToolExecution points back to
# the persisted ToolMessage).
from XBotv2.core.tools import ToolExecution as _ToolExecution
_ToolExecution.model_rebuild(_types_namespace={"ToolMessage": ToolMessage})


__all__ = [
    "AssistantContent",
    "AssistantMessage",
    "CompactionSummaryMessage",
    "ConversationMessage",
    "HumanInputMessage",
    "ImagePart",
    "ReasoningPart",
    "RuntimeNoticeMessage",
    "TextContent",
    "TextPart",
    "ToolMessage",
]
