"""Bounded display projections of canonical archived messages."""

import json

from XBotv2.core.api.messages import (
    AssistantMessage, CompactionSummaryMessage, HumanInputMessage,
    RuntimeNoticeMessage, ToolMessage,
)
from XBotv2.core.api.parts import MediaPart, ReasoningPart, TextPart
from XBotv2.core.api.tools import ToolFailed, ToolSucceeded
from XBotv2.session.api.records import DisplayBody, DisplayRecord

PREVIEW_CHARS = 512


def body_fields(message, args=None):
    if isinstance(message, (HumanInputMessage, RuntimeNoticeMessage)):
        return {"content": "".join(part.text for part in message.parts if isinstance(part, TextPart))}
    if isinstance(message, AssistantMessage):
        return {
            "content": "".join(part.text for part in message.parts if isinstance(part, TextPart)),
            "reasoning": "".join(part.text for part in message.parts if isinstance(part, ReasoningPart)),
        }
    if isinstance(message, CompactionSummaryMessage):
        return {"summary": message.summary}
    if isinstance(message, ToolMessage):
        result = ""
        if isinstance(message.outcome, (ToolSucceeded, ToolFailed)):
            result = "".join(part.text for part in message.outcome.output.parts if isinstance(part, TextPart))
            if not result and isinstance(message.outcome, ToolFailed):
                result = message.outcome.error.message
        elif hasattr(message.outcome, "reason"):
            result = message.outcome.reason
        return {"args": json.dumps(args, ensure_ascii=False, indent=2), "result": result}
    raise TypeError(f"Unsupported conversation message: {type(message).__name__}")


def display_record(message, args=None):
    fields = body_fields(message, args)
    media = tuple(part.artifact for part in getattr(message, "parts", ()) if isinstance(part, MediaPart))
    artifacts = getattr(message, "artifacts", ())
    if isinstance(message, ToolMessage) and isinstance(message.outcome, (ToolSucceeded, ToolFailed)):
        media = tuple(part.artifact for part in message.outcome.output.parts if isinstance(part, MediaPart))
        artifacts = message.outcome.output.artifacts
    return DisplayRecord(
        kind=message.kind, id=str(message.id),
        fields={name: DisplayBody(preview=value[:PREVIEW_CHARS], length=len(value)) for name, value in fields.items()},
        source=getattr(message, "source", ""), event=getattr(message, "event", ""),
        media=media, artifacts=artifacts,
        tool_name=message.call.name if isinstance(message, ToolMessage) else "",
        call_id=str(message.call.id) if isinstance(message, ToolMessage) else "",
        outcome=message.outcome.kind if isinstance(message, ToolMessage) else "",
        duration_ms=message.timing.duration_ms if isinstance(message, ToolMessage) else 0,
    )
