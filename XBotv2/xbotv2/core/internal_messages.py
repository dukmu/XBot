"""Structured envelopes for synthetic conversation messages."""

from __future__ import annotations

import json
from typing import Any

from xbotv2.api.messages import Message
from xbotv2.api.prompts import (
    CACHED_CONTENT_KEY,
    DISPLAY_CONTENT_KEY,
    MESSAGE_FORMAT_KEY,
    prompt_container,
    prompt_element,
)


def structure_tool_message(message: Message, tool_name: str) -> Message:
    """Wrap a runtime Tool message while preserving its protocol role."""
    if message.role != "tool":
        return message
    metadata = message.additional_kwargs
    if metadata.get(MESSAGE_FORMAT_KEY) == "xml-v1":
        return message

    content = str(message.content or "")
    data = metadata.get("xbotv2_data")
    error = metadata.get("xbotv2_error")
    children: list[str] = []

    if metadata.pop(CACHED_CONTENT_KEY, False):
        children.append(content)
    elif not _content_matches_data(content, data):
        if content:
            children.append(prompt_element("content", content))

    if data is not None:
        children.append(_json_element("data", data))
    if error is not None:
        children.append(_json_element("error", error))
    if message.artifact:
        children.append(_json_element("artifacts", _artifacts(message.artifact)))
    if not children:
        children.append(prompt_element("content", ""))

    message.content = prompt_container(
        "tool_result",
        children,
        attributes={
            "name": tool_name or "tool",
            "status": message.status or "success",
        },
    )
    message.name = tool_name or message.name
    metadata.pop(DISPLAY_CONTENT_KEY, None)
    metadata[MESSAGE_FORMAT_KEY] = "xml-v1"
    return message


def _content_matches_data(content: str, data: Any) -> bool:
    if data is None or not content:
        return False
    try:
        return json.loads(content) == data
    except (json.JSONDecodeError, TypeError):
        return False


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
    "CACHED_CONTENT_KEY",
    "DISPLAY_CONTENT_KEY",
    "MESSAGE_FORMAT_KEY",
    "structure_tool_message",
]
