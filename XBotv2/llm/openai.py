"""OpenAI-compatible provider adapter."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator
from openai.types.chat.chat_completion_chunk import ChoiceDelta
from openai.types.completion_usage import CompletionUsage
from pydantic import JsonValue

from XBotv2.core.provider import (
    ProviderAssistant,
    ProviderMessage,
    ProviderSystem,
    ProviderTool,
    ProviderUser,
    ResolvedImagePart,
    ModelRequest,
    ToolSchema,
)
from XBotv2.core.parts import ImagePart, ReasoningPart, TextPart
from XBotv2.core.stream import (
    ModelCompleted,
    ModelResponse,
    ModelStreamEvent,
    ToolCallDelta,
    ReasoningDelta,
    TextDelta,
)
from XBotv2.core.domain import (
    CompletedStop,
    LengthLimitedStop,
    MeasurementUnavailable,
    ModelStop,
    ProviderExtensions,
    ProviderMeasured,
    TokenCounters,
    ToolCallsRequestedStop,
    UsageDelta,
)
from XBotv2.core.tools import ToolCall
from XBotv2.core.providers import BaseProvider
from XBotv2.llm.config import merge_request_extras
from XBotv2.llm.client import _parse_tool_args, _provider_arguments
from XBotv2.llm.provider_errors import (
    normalize_sdk_provider_error,
    provider_context_overflow,
)
from XBotv2.llm.base import (
    attachment_prompt,
    tool_content,
    provider_usage,
    resolved_image_data,
)


@dataclass(slots=True)
class _ToolCallBuffer:
    call_id: str = ""
    name: str = ""
    argument_fragments: list[str] = field(default_factory=list)


class OpenAICompatibleProvider(BaseProvider):
    supported_input_modalities = frozenset({"text", "image"})

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None,
        extra_body: dict[str, JsonValue] | None = None,
        extra_headers: dict[str, str] | None = None,
        max_retries: int | None = None,
        retry_backoff_factor: float = 0.5,
        input_modalities: list[str] | None = None,
    ) -> None:
        from openai import AsyncOpenAI

        super().__init__(
            max_retries=max_retries,
            retry_backoff_factor=retry_backoff_factor,
            input_modalities=input_modalities,
        )
        kwargs: dict[str, JsonValue] = {"api_key": api_key, "max_retries": 0}
        if base_url:
            kwargs["base_url"] = base_url
        self._extra_body = {} if extra_body is None else dict(extra_body)
        if extra_headers:
            kwargs["default_headers"] = dict(extra_headers)
        self.client = AsyncOpenAI(**kwargs)

    # Documented OpenAI-compatible overflow discriminators.  llama.cpp reports
    # the oversized-context case as a 400 with this type instead of a code.
    _OVERFLOW_TYPES = frozenset({"exceed_context_size_error"})
    _OVERFLOW_CODES = frozenset({"context_length_exceeded", "prompt_too_long"})

    def normalize_provider_error(self, error: Exception):
        overflow = provider_context_overflow(
            error,
            types=self._OVERFLOW_TYPES,
            codes=self._OVERFLOW_CODES,
        )
        return overflow if overflow is not None else normalize_sdk_provider_error(error)

    async def _astream_once(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        selection = request.selection
        api_kwargs: dict[str, JsonValue] = {
            "model": selection.route.model,
            "messages": openai_messages(
                request.messages,
            ),
            "tools": [openai_tool_schema(tool) for tool in request.tools] or None,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        generation = selection.generation
        if generation.temperature is not None:
            api_kwargs["temperature"] = generation.temperature
        api_kwargs["max_tokens"] = generation.max_output_tokens
        if generation.mode.kind == "reasoning":
            api_kwargs["reasoning_effort"] = generation.mode.effort
        derived_extra_body: dict[str, JsonValue] = {}
        extra_body = merge_request_extras(
            derived_extra_body,
            self._extra_body,
        )
        if extra_body:
            api_kwargs["extra_body"] = extra_body
        response = await self.client.chat.completions.create(**api_kwargs)

        reasoning_parts: list[str] = []
        content_parts: list[str] = []
        tool_call_buffers: dict[int, _ToolCallBuffer] = {}
        final_usage: tuple[UsageDelta, object] | None = None
        stop_reason = ""

        async for chunk in response:
            usage = chunk.usage
            if usage is not None:
                final_usage = normalize_openai_usage(usage)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            reasoning = _openai_reasoning(delta)
            if reasoning:
                reasoning_parts.append(reasoning)
                yield ReasoningDelta(text=reasoning)

            content = delta.content if delta.content is not None else ""
            if content:
                content_parts.append(content)
                yield TextDelta(text=content)

            for tool_call in delta.tool_calls if delta.tool_calls is not None else ():
                index = tool_call.index
                buffer = tool_call_buffers.setdefault(
                    index,
                    _ToolCallBuffer(),
                )
                if tool_call.id is not None:
                    buffer.call_id = tool_call.id
                function = tool_call.function
                if function is not None:
                    if function.name is not None:
                        buffer.name = function.name
                    if function.arguments is not None:
                        buffer.argument_fragments.append(function.arguments)
                yield ToolCallDelta(
                    call_id=buffer.call_id,
                    name_delta=(
                        function.name if function.name is not None else ""
                    ) if function is not None else "",
                    arguments_delta=(
                        function.arguments
                        if function.arguments is not None else ""
                    ) if function is not None else "",
                )

            finish_reason = chunk.choices[0].finish_reason
            if finish_reason:
                stop_reason = finish_reason

        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        tool_calls = []
        for index, buffer in tool_call_buffers.items():
            if not buffer.call_id or not buffer.name:
                raise ValueError(
                    f"OpenAI stream ended with incomplete tool call at index {index}"
                )
            tool_calls.append(ToolCall(
                id=buffer.call_id,
                name=buffer.name,
                args=_parse_tool_args(
                    "".join(buffer.argument_fragments),
                    tool_name=buffer.name,
                ),
            ))
        parts = []
        if reasoning:
            parts.append(ReasoningPart(text=reasoning))
        if content:
            parts.append(TextPart(text=content))
        parts.extend(tool_calls)
        if final_usage is None:
            usage = UsageDelta(counters=TokenCounters())
            observed_context = MeasurementUnavailable(
                reason="provider did not report prompt tokens"
            )
        else:
            usage, observed_context = final_usage
        stop: ModelStop
        if stop_reason in {"tool_calls", "function_call"}:
            stop = ToolCallsRequestedStop()
        elif stop_reason in {"length", "max_tokens"}:
            stop = LengthLimitedStop(limit_kind="output_tokens")
        else:
            stop = CompletedStop()
        yield ModelCompleted(response=ModelResponse(
            parts=parts,
            usage=usage,
            observed_context=observed_context,
            stop=stop,
            provider_extensions=ProviderExtensions(
                provider="openai",
                payload={"model": selection.route.model},
            ),
        ))


def _openai_reasoning(delta: ChoiceDelta) -> str:
    """Read supported reasoning extensions at the OpenAI adapter boundary."""
    extensions = delta.model_extra
    if extensions is None:
        return ""
    for name in ("reasoning_content", "reasoning"):
        value = extensions.get(name)
        if value is None:
            continue
        if not isinstance(value, str):
            raise TypeError(f"OpenAI {name} extension must be text")
        if value:
            return value
    return ""


def openai_messages(
    messages: tuple[ProviderMessage, ...],
) -> list[dict[str, JsonValue]]:
    result: list[dict[str, JsonValue]] = []
    for message in messages:
        if isinstance(message, ProviderSystem):
            result.append({"role": "system", "content": "\n\n".join(part.text for part in message.parts)})
            continue
        if isinstance(message, ProviderTool):
            result.append(
                {
                    "role": "tool",
                    "content": tool_content(message),
                    "tool_call_id": message.call_id,
                }
            )
            continue
        role = "user" if isinstance(message, ProviderUser) else "assistant"
        parts = message.parts
        item: dict[str, JsonValue] = {
            "role": role,
            "content": _openai_content(
                parts,
                attachment_prompt(message),
            ),
        }
        tool_calls = [part for part in parts if isinstance(part, ToolCall)]
        if tool_calls:
            item["tool_calls"] = [
                openai_tool_call(tool_call) for tool_call in tool_calls
            ]
        result.append(item)
    return result


def _openai_content(
    parts: tuple,
    attachment_text: str = "",
) -> str | list[dict[str, JsonValue]]:
    text = "".join(
        part.text for part in parts if isinstance(part, TextPart)
    )
    if attachment_text:
        text = f"{text}\n\n{attachment_text}".strip()
    images = [part for part in parts if isinstance(part, ResolvedImagePart)]
    if not images:
        return text
    content_parts: list[dict[str, JsonValue]] = []
    if text:
        content_parts.append({"type": "text", "text": text})
    content_parts.extend({
        "type": "image_url",
        "image_url": {
            "url": (
                f"data:{image.ref.media_type};base64,"
                f"{resolved_image_data(image)}"
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


def openai_tool_schema(tool: ToolSchema) -> dict[str, JsonValue]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def normalize_openai_usage(usage: CompletionUsage):
    prompt_tokens = usage.prompt_tokens
    prompt_details = usage.prompt_tokens_details
    cache_read = (
        prompt_details.cached_tokens
        if prompt_details is not None
        else None
    )
    cache_read_field = "prompt_tokens_details.cached_tokens"
    extensions = usage.model_extra
    if cache_read is None and extensions is not None:
        cache_read = extensions.get("prompt_cache_hit_tokens")
        cache_read_field = "prompt_cache_hit_tokens"
    if cache_read is None and extensions is not None:
        cache_read = extensions.get("cache_read_input_tokens")
        cache_read_field = "cache_read_input_tokens"
    cache_creation = (
        extensions.get("cache_creation_input_tokens")
        if extensions is not None
        else None
    )
    prompt_cache_write = (
        extensions.get("prompt_cache_write_tokens")
        if extensions is not None
        else None
    )
    cache_read = _provider_counter(cache_read, cache_read_field)
    cache_creation = _provider_counter(
        cache_creation,
        "cache_creation_input_tokens",
    )
    prompt_cache_write = _provider_counter(
        prompt_cache_write,
        "prompt_cache_write_tokens",
    )
    input_tokens = prompt_tokens - cache_read - cache_creation
    if input_tokens < 0:
        raise ValueError(
            "OpenAI cache token counters exceed prompt_tokens"
        )
    return provider_usage(
        input_tokens=input_tokens,
        output_tokens=usage.completion_tokens,
        context_tokens=prompt_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
        prompt_cache_write_tokens=prompt_cache_write,
    )


def _provider_counter(value: object, name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"OpenAI {name} must be a non-negative integer")
    return value


__all__ = ["OpenAICompatibleProvider"]


def create_openai_provider(provider_config, model_config):
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
        extra_headers=provider_config.headers or None,
    )


__all__ = ["OpenAICompatibleProvider", "create_openai_provider"]
