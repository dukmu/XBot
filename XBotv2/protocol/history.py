"""Display-safe serialization for provider-neutral conversation history."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from api.messages import Message
from api.prompts import MESSAGE_FORMAT_KEY, tool_result_display_content


def display_history(messages: Iterable[Message]) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for message in messages:
        if message.role not in {"user", "assistant", "tool"}:
            continue
        additional = message.additional_kwargs or {}
        content = str(message.content or "")
        if (
            message.role == "tool"
            and additional.get(MESSAGE_FORMAT_KEY)
        ):
            content = tool_result_display_content(content)
        item = {
            "role": message.role,
            "content": content,
            "tool_calls": [call.to_dict() for call in message.tool_calls or []],
            "tool_call_id": message.tool_call_id or "",
            "status": message.status or "",
            "images": [image.to_dict() for image in message.images],
            "artifacts": [
                data
                for value in message.artifact or []
                if (data := _artifact_data(value)) is not None
            ],
        }
        if message.role == "tool":
            item.update({
                "data": message.data,
                "error": message.error,
            })
        runtime = additional.get("runtime_input")
        if isinstance(runtime, dict):
            item["runtime"] = {
                str(key): str(value)
                for key, value in runtime.items()
            }
        history.append(item)
    return history


def _artifact_data(value: Any) -> dict[str, Any] | None:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return dict(value) if isinstance(value, dict) else None


__all__ = ["display_history"]
