"""Provider-adapter rendering helpers."""

from __future__ import annotations

import base64
from pathlib import Path
from collections.abc import Mapping
from xml.etree import ElementTree

from XBotv2.core.parts import TextPart
from XBotv2.core.provider import ProviderMessage, ProviderTool, ResolvedImagePart
from XBotv2.core.prompts import prompt_container, prompt_element
from XBotv2.core.domain import (
    MeasurementUnavailable,
    ObservedContext,
    ProviderMeasured,
    TokenCounters,
    UsageDelta,
)


def tool_content(message: ProviderTool) -> str:
    """Render the already-compiled provider tool result."""
    return "".join(part.text for part in message.parts if isinstance(part, TextPart))


def resolved_image_data(image: ResolvedImagePart) -> str:
    """Encode bytes from the path already resolved by the context compiler."""
    return base64.b64encode(Path(image.absolute_path).read_bytes()).decode("ascii")


def attachment_prompt(message: ProviderMessage) -> str:
    """Render uploaded file references without embedding their bytes."""
    children = []
    images = [part for part in message.parts if isinstance(part, ResolvedImagePart)]
    for image in images:
        item = image.ref.model_dump(mode="json")
        path = image.absolute_path
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


def provider_usage(
    *,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int | None = None,
    context_tokens: int | None = None,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    prompt_cache_write_tokens: int = 0,
) -> tuple[UsageDelta, ObservedContext]:
    """Map provider accounting directly to the canonical usage values."""
    del total_tokens
    delta = UsageDelta(counters=TokenCounters(
        input=input_tokens,
        output=output_tokens,
        cache_read=cache_read_input_tokens,
        cache_create=cache_creation_input_tokens,
        prompt_cache_write=prompt_cache_write_tokens,
    ))
    observed: ObservedContext = (
        ProviderMeasured(tokens=context_tokens)
        if context_tokens is not None
        else MeasurementUnavailable(reason="provider did not report prompt tokens")
    )
    return delta, observed
