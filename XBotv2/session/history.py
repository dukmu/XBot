"""Session-owned display projection for conversation history."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from XBotv2.core.artifacts import ArtifactRef
from XBotv2.core.messages import Message
from XBotv2.core.prompts import MESSAGE_FORMAT_KEY, tool_result_display_content
from XBotv2.core.tools import JsonObject


@dataclass(frozen=True, slots=True)
class ConversationReplayItem:
    """Transport-neutral projection of one visible conversation record."""

    role: Literal["user", "assistant", "tool"]
    content: str
    reasoning: str
    tool_calls: tuple[JsonObject, ...]
    tool_call_id: str
    input_id: str
    status: str
    data: Any
    images: tuple[JsonObject, ...]
    artifacts: tuple[JsonObject, ...]
    error: JsonObject | None
    runtime: dict[str, str] | None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "role": self.role,
            "content": self.content,
            "tool_calls": list(self.tool_calls),
            "tool_call_id": self.tool_call_id,
            "status": self.status,
            "data": self.data,
            "images": list(self.images),
            "artifacts": list(self.artifacts),
        }
        if self.reasoning:
            value["reasoning"] = self.reasoning
        if self.role == "tool":
            value["error"] = self.error
        if self.runtime is not None:
            value["runtime"] = self.runtime
        return value


def conversation_replay(
    messages: Iterable[Message],
) -> tuple[ConversationReplayItem, ...]:
    replay: list[ConversationReplayItem] = []
    for message in messages:
        if message.role not in {"user", "assistant", "tool"}:
            continue
        additional = message.additional_kwargs or {}
        content = str(message.content or "")
        if message.role == "tool" and additional.get(MESSAGE_FORMAT_KEY):
            content = tool_result_display_content(content)
        runtime_value = additional.get("runtime_input")
        runtime = (
            {str(key): str(value) for key, value in runtime_value.items()}
            if isinstance(runtime_value, dict)
            else None
        )
        replay.append(ConversationReplayItem(
            role=message.role,
            content=content,
            reasoning=message.reasoning if message.role == "assistant" else "",
            tool_calls=tuple(call.to_dict() for call in message.tool_calls or []),
            tool_call_id=message.tool_call_id or "",
            input_id=message.input_id or "",
            status=message.status or "",
            data=message.data,
            images=tuple(image.to_dict() for image in message.images),
            artifacts=tuple(
                data
                for value in message.artifact or []
                if (data := _artifact_data(value)) is not None
            ),
            error=message.error if message.role == "tool" else None,
            runtime=runtime,
        ))
    return tuple(replay)


def display_history(messages: Iterable[Message]) -> list[dict[str, Any]]:
    return [item.to_dict() for item in conversation_replay(messages)]


def _artifact_data(value: Any) -> dict[str, Any] | None:
    if isinstance(value, ArtifactRef):
        return value.to_dict()
    return dict(value) if isinstance(value, Mapping) else None


__all__ = [
    "ConversationReplayItem",
    "conversation_replay",
    "display_history",
]
