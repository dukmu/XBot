"""Session-owned display projection for conversation history."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from operator import not_
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from XBotv2.core.artifacts import ArtifactRef
from XBotv2.core.messages import Message
from XBotv2.core.prompts import MESSAGE_FORMAT_KEY, tool_result_display_content
from XBotv2.core.timing import TIMING_METADATA_KEY


class SessionHistoryItem(BaseModel):
    """Transport-neutral projection of one visible conversation record."""

    role: Literal["user", "assistant", "tool"]
    content: str
    content: str = ""
    reasoning: str = Field(default="", exclude_if=not_)
    tool_calls: tuple[dict[str, JsonValue], ...] = ()
    tool_call_id: str = ""
    input_id: str = Field(default="", exclude=True)
    status: str = ""
    data: JsonValue = None
    images: tuple[dict[str, JsonValue], ...] = ()
    artifacts: tuple[dict[str, JsonValue], ...] = ()
    error: dict[str, JsonValue] | None = None
    runtime: dict[str, str] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    timing: dict[str, JsonValue] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    model_config = ConfigDict(extra="forbid", frozen=True)


def conversation_replay(
    messages: Iterable[Message],
) -> tuple[SessionHistoryItem, ...]:
    replay: list[SessionHistoryItem] = []
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
        replay.append(SessionHistoryItem(
            role=message.role,
            content=content,
            reasoning=message.reasoning if message.role == "assistant" else "",
            tool_calls=tuple(
                call.model_dump(mode="json") for call in message.tool_calls or []
            ),
            tool_call_id=message.tool_call_id or "",
            input_id=message.input_id or "",
            status=message.status or "",
            data=message.data,
            images=tuple(image.model_dump(mode="json") for image in message.images),
            artifacts=tuple(
                data
                for value in message.artifact or []
                if (data := _artifact_data(value)) is not None
            ),
            error=message.error if message.role == "tool" else None,
            runtime=runtime,
            timing=(
                dict(timing)
                if isinstance(
                    timing := message.response_metadata.get(TIMING_METADATA_KEY),
                    Mapping,
                )
                else None
            ),
        ))
    return tuple(replay)


def _artifact_data(value: Any) -> dict[str, Any] | None:
    if isinstance(value, ArtifactRef):
        return value.model_dump(mode="json")
    return dict(value) if isinstance(value, Mapping) else None


__all__ = [
    "SessionHistoryItem",
    "conversation_replay",
]
