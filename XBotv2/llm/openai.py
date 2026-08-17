"""OpenAI-compatible provider adapter."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Callable

from XBotv2.core.messages import (
    ContentPart,
    ImagePart,
    Message,
    ModelChunk,
    ModelResponse,
    ReasoningPart,
    TextPart,
    ToolCallPart,
)
from XBotv2.core.tools import ToolCall, ToolCallDelta
from XBotv2.core.providers import BaseProvider
from XBotv2.llm.base import (
    attachment_prompt,
    usage_metadata,
)

class OpenAICompatibleProvider(BaseProvider):
    supported_input_modalities = frozenset({"text", "image"})

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None,
        temperature: float,
        max_output_tokens: int | None,
        reasoning_effort: str | None = None,
        thinking_enabled: bool = False,
        max_retries: int | None = None,
        retry_backoff_factor: float = 0.5,
        input_modalities: list[str] | None = None,
        media_root: str | None = None,
    ) -> None:
        from openai import AsyncOpenAI

        super().__init__(
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            thinking_enabled=thinking_enabled,
            max_retries=max_retries,
            retry_backoff_factor=retry_backoff_factor,
            input_modalities=input_modalities,
            media_root=media_root,
        )
        kwargs: dict[str, Any] = {"api_key": api_key, "max_retries": 0}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = AsyncOpenAI(**kwargs)

    async def _astream_once(
        self,
        messages: list[Message],
        **_kwargs: Any,
    ) -> AsyncIterator[ModelChunk]:
        api_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": openai_messages(
                messages,
                image_loader=self.read_image,
            ),
            "tools": self.bound_tools or None,
            "temperature": self.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.max_output_tokens is not None:
            api_kwargs["max_tokens"] = self.max_output_tokens
        if self.reasoning_effort:
            api_kwargs["reasoning_effort"] = self.reasoning_effort
        if self.thinking_enabled:
            api_kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        response = await self.client.chat.completions.create(**api_kwargs)

        reasoning_parts: list[str] = []
        content_parts: list[str] = []
        tool_call_buffers: dict[int, dict[str, Any]] = {}
        final_usage: dict[str, int] = {}
        stop_reason = ""

        async for chunk in response:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                final_usage = normalize_openai_usage(usage)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue

            reasoning = getattr(delta, "reasoning_content", None)
            if not reasoning:
                reasoning = getattr(delta, "reasoning", None)
            reasoning = reasoning or ""
            if reasoning:
                reasoning_parts.append(reasoning)
                yield ModelChunk(
                    reasoning=reasoning,
                )

            content = getattr(delta, "content", None) or ""
            if content:
                content_parts.append(content)
                yield ModelChunk(content=content)

            for tool_call in getattr(delta, "tool_calls", None) or []:
                index = getattr(tool_call, "index", 0)
                buffer = tool_call_buffers.setdefault(
                    index,
                    {
                        "id": getattr(tool_call, "id", "") or "",
                        "name": "",
                        "args": "",
                    },
                )
                if getattr(tool_call, "id", None):
                    buffer["id"] = tool_call.id
                function = getattr(tool_call, "function", None)
                if function:
                    if getattr(function, "name", None):
                        buffer["name"] = function.name
                    if getattr(function, "arguments", None):
                        buffer["args"] += function.arguments
                yield ModelChunk(
                    tool_call_chunks=[
                        ToolCallDelta(
                            index=index,
                            id=buffer["id"],
                            name=buffer["name"],
                            args=buffer["args"],
                        )
                    ]
                )

            finish_reason = getattr(chunk.choices[0], "finish_reason", None)
            if finish_reason:
                stop_reason = str(finish_reason)

        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        tool_calls = [
            ToolCall(
                id=buffer["id"],
                name=buffer["name"],
                args=_parse_tool_args(buffer["args"]),
            )
            for buffer in tool_call_buffers.values()
            if buffer["name"]
        ]
        parts = []
        if reasoning:
            parts.append(ReasoningPart(reasoning))
        if content:
            parts.append(TextPart(content))
        parts.extend(ToolCallPart(call) for call in tool_calls)
        yield ModelResponse(
            parts=parts,
            response_metadata={
                "model_name": self.model,
                **({"stop_reason": stop_reason} if stop_reason else {}),
            },
            usage_metadata=final_usage,
        )


def openai_messages(
    messages: list[Message],
    *,
    image_loader: Callable[[str], str] | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    system_parts = [
        message.content
        for message in messages
        if message.role == "system" and message.content.strip()
    ]
    if system_parts:
        result.append({"role": "system", "content": "\n\n".join(system_parts)})
    for message in messages:
        role = message.role
        if role == "system":
            continue
        content = message.content
        if role == "tool":
            if message.images:
                raise ValueError(
                    "OpenAI Chat Completions supports image content only in user messages"
                )
            result.append(
                {
                    "role": "tool",
                    "content": str(content),
                    "tool_call_id": message.tool_call_id,
                }
            )
            continue
        parts = message.parts
        images = [part for part in parts if isinstance(part, ImagePart)]
        if images and role != "user":
            raise ValueError("Image content is supported only in user messages")
        item: dict[str, Any] = {
            "role": role,
            "content": _openai_content(
                parts,
                image_loader,
                attachment_prompt(message),
            ),
        }
        tool_calls = [
            part.call for part in parts if isinstance(part, ToolCallPart)
        ]
        if tool_calls:
            item["tool_calls"] = [
                openai_tool_call(tool_call) for tool_call in tool_calls
            ]
        result.append(item)
    return result


def _openai_content(
    parts: list[ContentPart],
    image_loader: Callable[[str], str] | None,
    attachment_text: str = "",
) -> str | list[dict[str, Any]]:
    text = "".join(
        part.text for part in parts if isinstance(part, TextPart)
    )
    if attachment_text:
        text = f"{text}\n\n{attachment_text}".strip()
    images = [part.image for part in parts if isinstance(part, ImagePart)]
    if not images:
        return text
    if image_loader is None:
        raise ValueError("Image loader is required for image content")
    content_parts: list[dict[str, Any]] = []
    if text:
        content_parts.append({"type": "text", "text": text})
    content_parts.extend({
        "type": "image_url",
        "image_url": {
            "url": (
                f"data:{image.media_type};base64,"
                f"{image_loader(image.path)}"
            )
        },
    } for image in images)
    return content_parts


def openai_tool_call(tool_call: ToolCall) -> dict[str, Any]:
    return {
        "id": tool_call.id,
        "type": "function",
        "function": {
            "name": tool_call.name,
            "arguments": json.dumps(tool_call.args, ensure_ascii=False),
        },
    }


def normalize_openai_usage(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    reported_total = getattr(usage, "total_tokens", None)
    cache_read = getattr(usage, "prompt_cache_hit_tokens", None)
    if cache_read is None:
        cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_creation = getattr(usage, "prompt_cache_miss_tokens", None)
    if cache_creation is None:
        cache_creation = getattr(usage, "cache_creation_input_tokens", 0)
    cache_read = int(cache_read or 0)
    cache_creation = int(cache_creation or 0)
    return usage_metadata(
        input_tokens=max(0, prompt_tokens - cache_read - cache_creation),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        total_tokens=(
            int(reported_total) if reported_total is not None else None
        ),
        context_tokens=prompt_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
        prompt_cache_write_tokens=int(
            getattr(usage, "prompt_cache_write_tokens", 0) or 0
        ),
    )


def _parse_tool_args(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


__all__ = ["OpenAICompatibleProvider"]


def create_openai_provider(provider_config, *, media_root=None):
    """Factory for the openai-compatible route (openai / deepseek / lmstudio-openai)."""
    from XBotv2.llm.config import expand_env
    from XBotv2.llm.client import _require_api_key, _retry_settings

    provider = provider_config.provider
    api_key = expand_env(provider_config.api_key or "")
    base_url = (
        expand_env(provider_config.base_url)
        if provider_config.base_url
        else None
    )
    _require_api_key(provider, provider_config.model, api_key)
    max_retries, retry_backoff_factor = _retry_settings()
    logging.getLogger("llm").info(
        "creating openai-compatible provider=%s model=%s", provider, provider_config.model
    )
    return OpenAICompatibleProvider(
        model=provider_config.model,
        api_key=api_key,
        base_url=base_url,
        temperature=provider_config.temperature,
        max_output_tokens=provider_config.max_output_tokens,
        reasoning_effort=provider_config.reasoning_effort,
        thinking_enabled=provider_config.thinking_enabled,
        max_retries=max_retries,
        retry_backoff_factor=retry_backoff_factor,
        input_modalities=provider_config.input_modalities,
        media_root=media_root,
    )


__all__ = [*globals().get("__all__", []), "create_openai_provider"]
