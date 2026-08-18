"""Structured envelopes for synthetic conversation messages."""

from __future__ import annotations

import json
from typing import Any

from XBotv2.core.messages import Message
from XBotv2.core.prompts import (
    CACHED_CONTENT_KEY,
    DISPLAY_CONTENT_KEY,
    MESSAGE_FORMAT_KEY,
    prompt_container,
    prompt_element,
)


def structure_tool_message(message: Message, tool_name: str) -> Message:
    """Structure errors and artifacts without copying sidecar data into context."""
    if message.role != "tool":
        return message
    metadata = message.additional_kwargs
    if metadata.get(MESSAGE_FORMAT_KEY):
        return message

    content = str(message.content or "")
    error = message.error
    cached = metadata.pop(CACHED_CONTENT_KEY, False)
    status = message.status or "success"
    message.name = tool_name or message.name
    if cached:
        metadata.pop(DISPLAY_CONTENT_KEY, None)
        metadata[MESSAGE_FORMAT_KEY] = "xml"
        return message
    if (
        status == "success"
        and error is None
        and not message.artifact
        and content
    ):
        return message
    children: list[str] = []

    if content:
        children.append(prompt_element("content", content))
    if message.artifact:
        children.append(_json_element("artifacts", _artifacts(message.artifact)))
    if error is not None:
        children.append(_json_element("error", error))
    message.content = prompt_container(
        "tool_result",
        children,
        attributes={
            "name": tool_name or "tool",
            "status": status,
        },
    )
    metadata.pop(DISPLAY_CONTENT_KEY, None)
    metadata[MESSAGE_FORMAT_KEY] = "xml"
    return message


def _json_element(name: str, value: Any) -> str:
    return prompt_element(
        name,
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str),
        attributes={"encoding": "json"},
    )


def _artifacts(value: Any) -> list[Any]:
    values = value if isinstance(value, (list, tuple)) else [value]
    return [_artifact_value(item) for item in values]


def _artifact_value(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value if isinstance(value, dict) else str(value)


__all__ = [
    "structure_tool_message",
]
