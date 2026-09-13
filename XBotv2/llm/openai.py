"""OpenAI-compatible provider adapter."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, AsyncIterator, Callable
from pydantic import JsonValue

from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.messages import (
    ContentPart,
    ImagePart,
    Message,
    ModelChunk,
    ModelResponse,
    ReasoningPart,
    TextPart,
)
from XBotv2.core.tools import ToolCall, ToolCallDelta
from XBotv2.core.providers import (
    BaseProvider,
    ModelRequestOptions,
    provider_context_overflow,
)
from XBotv2.llm.config import merge_request_extras
from XBotv2.llm.client import _parse_tool_args, _provider_arguments
from XBotv2.llm.base import (
    attachment_prompt,
    tool_content,
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
        temperature: float | None,
        max_output_tokens: int | None,
        reasoning_effort: str | None = None,
        thinking: str | None = None,
        extra_body: dict[str, JsonValue] | None = None,
        extra_headers: dict[str, str] | None = None,
        max_retries: int | None = None,
        retry_backoff_factor: float = 0.5,
        input_modalities: list[str] | None = None,
        artifacts: ArtifactStorePort | None = None,
    ) -> None:
        from openai import AsyncOpenAI

        super().__init__(
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            thinking=thinking,
            max_retries=max_retries,
            retry_backoff_factor=retry_backoff_factor,
            input_modalities=input_modalities,
            artifacts=artifacts,
        )
        kwargs: dict[str, JsonValue] = {"api_key": api_key, "max_retries": 0}
        if base_url:
            kwargs["base_url"] = base_url
        self._extra_body = dict(extra_body or {})
        if extra_headers:
            kwargs["default_headers"] = dict(extra_headers)
        self.client = AsyncOpenAI(**kwargs)

    # Documented OpenAI-compatible overflow discriminators.  llama.cpp reports
    # the oversized-context case as a 400 with this type instead of a code.
    _OVERFLOW_TYPES = frozenset({"exceed_context_size_error"})
    _OVERFLOW_CODES = frozenset({"context_length_exceeded", "prompt_too_long"})

    def normalize_provider_error(self, error: Exception) -> Exception:
        return provider_context_overflow(
            error,
            types=self._OVERFLOW_TYPES,
            codes=self._OVERFLOW_CODES,
        ) or error

    async def _astream_once(
        self,
        messages: list[Message],
        *,
        options: ModelRequestOptions | None = None,
    ) -> AsyncIterator[ModelChunk]:
        api_kwargs: dict[str, JsonValue] = {
            "model": self.model,
            "messages": openai_messages(
                messages,
                image_loader=self.read_image,
                artifacts=self.artifacts,
            ),
            "tools": self.bound_tools or None,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.temperature is not None:
            api_kwargs["temperature"] = self.temperature
        output_tokens = (
            options.max_output_tokens
            if options is not None and options.max_output_tokens is not None
            else self.max_output_tokens
        )
        if output_tokens is not None:
            api_kwargs["max_tokens"] = output_tokens
        if self.reasoning_effort:
            api_kwargs["reasoning_effort"] = self.reasoning_effort
        derived_extra_body: dict[str, JsonValue] = {}
        if self.thinking:
            derived_extra_body["thinking"] = {"type": self.thinking}
        extra_body = merge_request_extras(
            derived_extra_body,
            getattr(self, "_extra_body", {}),
        )
        if extra_body:
            api_kwargs["extra_body"] = extra_body
        response = await self.client.chat.completions.create(**api_kwargs)

        reasoning_parts: list[str] = []
        content_parts: list[str] = []
        tool_call_buffers: dict[int, dict[str, JsonValue]] = {}
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
            parts.append(ReasoningPart(text=reasoning))
        if content:
            parts.append(TextPart(text=content))
        parts.extend(
            ToolCall.model_validate(call, from_attributes=True)
            for call in tool_calls
        )
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
    artifacts: ArtifactStorePort | None = None,
) -> list[dict[str, JsonValue]]:
    result: list[dict[str, JsonValue]] = []
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
                    "content": tool_content(message, artifacts),
                    "tool_call_id": message.tool_call_id,
                }
            )
            continue
        parts = message.parts
        images = [part for part in parts if isinstance(part, ImagePart)]
        if images and role != "user":
            raise ValueError("Image content is supported only in user messages")
        item: dict[str, JsonValue] = {
            "role": role,
            "content": _openai_content(
                parts,
                image_loader,
                attachment_prompt(message, artifacts),
            ),
        }
        tool_calls = [
            part for part in parts if isinstance(part, ToolCall)
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
) -> str | list[dict[str, JsonValue]]:
    text = "".join(
        part.text for part in parts if isinstance(part, TextPart)
    )
    if attachment_text:
        text = f"{text}\n\n{attachment_text}".strip()
    images = [part for part in parts if isinstance(part, ImagePart)]
    if not images:
        return text
    if image_loader is None:
        raise ValueError("Image loader is required for image content")
    content_parts: list[dict[str, JsonValue]] = []
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


def openai_tool_call(tool_call: ToolCall) -> dict[str, JsonValue]:
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
    def value(name: str, default: Any = None) -> Any:
        if isinstance(usage, Mapping):
            return usage.get(name, default)
        return getattr(usage, name, default)

    prompt_tokens = int(value("prompt_tokens", 0) or 0)
    reported_total = value("total_tokens")
    details = value("prompt_tokens_details")
    if isinstance(details, Mapping):
        cache_read = details.get("cached_tokens")
    else:
        cache_read = getattr(details, "cached_tokens", None)
    if cache_read is None:
        cache_read = value("prompt_cache_hit_tokens")
    if cache_read is None:
        cache_read = value("cache_read_input_tokens", 0)
    cache_creation = value("cache_creation_input_tokens", 0)
    cache_read = int(cache_read or 0)
    cache_creation = int(cache_creation or 0)
    return usage_metadata(
        input_tokens=max(0, prompt_tokens - cache_read - cache_creation),
        output_tokens=int(value("completion_tokens", 0) or 0),
        total_tokens=(
            int(reported_total) if reported_total is not None else None
        ),
        context_tokens=prompt_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
        prompt_cache_write_tokens=int(
            value("prompt_cache_write_tokens", 0) or 0
        ),
    )


__all__ = ["OpenAICompatibleProvider"]


def create_openai_provider(provider_config, model_config, *, artifacts=None):
    """Factory for the openai-compatible protocol route.

    ``provider_config`` is the adapter instance (endpoint + credentials);
    ``model_config`` is the selected specific model from its catalog.
    """
    protocol = provider_config.protocol
    logging.getLogger("xbotv2.llm").info(
        "creating openai-compatible provider=%s model=%s",
        protocol, model_config.model,
    )
    return OpenAICompatibleProvider(
        **_provider_arguments(provider_config, model_config),
        artifacts=artifacts,
        extra_headers=provider_config.headers or None,
    )


__all__ = ["OpenAICompatibleProvider", "create_openai_provider"]
