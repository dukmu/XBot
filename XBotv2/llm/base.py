"""Provider-adapter rendering helpers."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Mapping
from typing import Any

from XBotv2.core.artifacts import ArtifactRef
from XBotv2.core.messages import Message
from XBotv2.core.prompts import prompt_container, prompt_element
from XBotv2.core.usage import UsageData


def attachment_prompt(message: Message) -> str:
    """Render uploaded file references without embedding their bytes."""
    children = []
    for value in message.artifact or []:
        if isinstance(value, ArtifactRef):
            item = value.model_dump(mode="json")
        elif isinstance(value, Mapping):
            item = dict(value)
        else:
            raise TypeError(f"Unsupported attachment reference: {type(value).__name__}")
        if not item.get("id"):
            raise ValueError("Attachment reference requires an id")
        path = str(item["id"])
        if not path.startswith("session/"):
            path = f"session/{path}"
        children.append(prompt_element(
            "attachment",
            "Use filesystem or shell tools to inspect this file when needed.",
            attributes={
                "name": item.get("name") or Path(path).name,
                "media_type": item.get("media_type") or "application/octet-stream",
                "path": path,
                "size": item.get("size"),
            },
        ))
    return prompt_container("attachments", children) if children else ""


def usage_metadata(
    *,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int | None = None,
    context_tokens: int | None = None,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    prompt_cache_write_tokens: int = 0,
) -> dict[str, int]:
    """Build the normalized per-request usage contract consumed by Core."""
    values: dict[str, int] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "requests": 1,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "prompt_cache_write_tokens": prompt_cache_write_tokens,
    }
    if total_tokens is not None:
        values["total_tokens"] = total_tokens
    if context_tokens is not None:
        values["context_tokens"] = context_tokens
    return UsageData.from_provider(values).to_event_dict()
