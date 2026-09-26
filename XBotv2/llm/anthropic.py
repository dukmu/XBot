"""Anthropic provider adapter."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import AsyncIterator
from anthropic.types import MessageDeltaUsage, Usage
from pydantic import JsonValue

from XBotv2.core.provider import (
    ProviderMessage,
    ProviderUser,
    ProviderAssistant,
    ProviderSystem,
    ProviderTool,
    ResolvedImagePart,
    ModelRequest,
    ToolSchema,
)
from XBotv2.core.parts import (
    ImagePart as CanonicalImagePart,
    ReasoningPart as CanonicalReasoningPart,
    TextPart as CanonicalTextPart,
)
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
    ModelStop,
    ProviderExtensions,
    ProviderMeasured,
    TokenCounters,
    ToolCallsRequestedStop,
    UsageDelta,
)
from XBotv2.core.tools import ToolCall
from XBotv2.core.providers import BaseProvider
from XBotv2.llm.base import (
    attachment_prompt,
    provider_usage,
    resolved_image_data,
    tool_content,
)
from XBotv2.llm.config import merge_request_extras
from XBotv2.llm.client import _parse_tool_args, _provider_arguments
from XBotv2.llm.provider_errors import (
    normalize_sdk_provider_error,
    provider_context_overflow,
)


@dataclass(slots=True)
class _UsageAccumulator:
    input_tokens: int | None = None
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def merge(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int,
        cache_read_input_tokens: int | None = None,
        cache_creation_input_tokens: int | None = None,
    ) -> None:
        if input_tokens is not None:
            self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        if cache_read_input_tokens is not None:
            self.cache_read_input_tokens = cache_read_input_tokens
        if cache_creation_input_tokens is not None:
            self.cache_creation_input_tokens = cache_creation_input_tokens


@dataclass(slots=True)
class _TextBlockState:
    text: str


@dataclass(slots=True)
class _ThinkingBlockState:
    text: str
    signature: str = ""


@dataclass(slots=True)
class _RedactedThinkingBlockState:
    data: str


@dataclass(slots=True)
class _ToolUseBlockState:
    call_id: str
    name: str
    initial_input: dict[str, object]
    argument_fragments: list[str]
    arguments: dict[str, object] | None = None


_AnthropicBlockState = (
    _TextBlockState
    | _ThinkingBlockState
    | _RedactedThinkingBlockState
    | _ToolUseBlockState
)


class AnthropicProvider(BaseProvider):
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
        from anthropic import AsyncAnthropic

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
        self.client = AsyncAnthropic(**kwargs)

    # Anthropic reports an oversized prompt as a 400 invalid_request_error
    # whose message names the limit, and as a 413 request_too_large body.
    _OVERFLOW_TYPES = frozenset({"request_too_large"})
    _OVERFLOW_STATUSES = frozenset({413})
    _OVERFLOW_MESSAGE_PREFIXES = (
        "prompt is too long",
        "input tokens exceed",
    )

    def normalize_provider_error(self, error: Exception):
        overflow = provider_context_overflow(
            error,
            types=self._OVERFLOW_TYPES,
            statuses=self._OVERFLOW_STATUSES,
            message_prefixes=self._OVERFLOW_MESSAGE_PREFIXES,
        )
        return overflow if overflow is not None else normalize_sdk_provider_error(error)

    async def _astream_once(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        selection = request.selection
        system, request_messages = anthropic_request_messages(
            request.messages,
        )
        api_kwargs: dict[str, JsonValue] = {
            "model": selection.route.model,
            "messages": request_messages,
            "max_tokens": selection.generation.max_output_tokens,
        }
        if selection.generation.temperature is not None:
            api_kwargs["temperature"] = selection.generation.temperature
        if system:
            api_kwargs["system"] = system
        if request.tools:
            api_kwargs["tools"] = [
                anthropic_tool_schema(tool) for tool in request.tools
            ]
        derived_extra_body: dict[str, JsonValue] = {}
        if selection.generation.mode.kind == "reasoning":
            derived_extra_body["reasoning_effort"] = selection.generation.mode.effort
        extra_body = merge_request_extras(
            derived_extra_body,
            self._extra_body,
        )
        if extra_body:
            api_kwargs["extra_body"] = extra_body

        content_blocks: dict[int, _AnthropicBlockState] = {}
        usage_values = _UsageAccumulator()
        response_model = selection.route.model
        stop_reason = ""

        stream = await self.client.messages.create(stream=True, **api_kwargs)
        try:
            async for event in stream:
                event_type = event.type
                if event_type == "message_start":
                    message = event.message
                    response_model = message.model
                    _merge_anthropic_usage(usage_values, message.usage)
                elif event_type == "content_block_start":
                    block = event.content_block
                    index = event.index
                    block_type = block.type
                    if block_type == "tool_use":
                        content_blocks[index] = _ToolUseBlockState(
                            call_id=block.id,
                            name=block.name,
                            initial_input=block.input,
                            argument_fragments=[],
                        )
                    elif block_type == "text":
                        # Anthropic-compatible streams may use null to mean
                        # "no initial fragment" and deliver the text entirely
                        # through subsequent deltas.
                        text = block.text or ""
                        content_blocks[index] = _TextBlockState(text=text)
                        if text:
                            yield TextDelta(text=text)
                    elif block_type == "thinking":
                        thinking = block.thinking or ""
                        content_blocks[index] = _ThinkingBlockState(
                            text=thinking,
                            signature=block.signature or "",
                        )
                        if thinking:
                            yield ReasoningDelta(text=thinking)
                    elif block_type == "redacted_thinking":
                        content_blocks[index] = _RedactedThinkingBlockState(
                            data=block.data,
                        )
                elif event_type == "content_block_delta":
                    index = event.index
                    delta = event.delta
                    delta_type = delta.type
                    block = content_blocks.get(index)
                    if block is None:
                        raise ValueError(
                            "Anthropic content delta arrived before its block start"
                        )
                    if delta_type == "input_json_delta":
                        if not isinstance(block, _ToolUseBlockState):
                            raise ValueError(
                                "Anthropic input JSON delta belongs to a non-tool block"
                            )
                        block.argument_fragments.append(delta.partial_json)
                    elif delta_type == "text_delta":
                        if not isinstance(block, _TextBlockState):
                            raise ValueError(
                                "Anthropic text delta belongs to a non-text block"
                            )
                        text = delta.text
                        if text:
                            block.text += text
                            yield TextDelta(text=text)
                    elif delta_type == "thinking_delta":
                        if not isinstance(block, _ThinkingBlockState):
                            raise ValueError(
                                "Anthropic thinking delta belongs to a non-thinking block"
                            )
                        thinking = delta.thinking
                        if thinking:
                            block.text += thinking
                            yield ReasoningDelta(text=thinking)
                    elif delta_type == "signature_delta":
                        if not isinstance(block, _ThinkingBlockState):
                            raise ValueError(
                                "Anthropic signature delta belongs to a non-thinking block"
                            )
                        if delta.signature:
                            block.signature += delta.signature
                elif event_type == "content_block_stop":
                    index = event.index
                    block = content_blocks.get(index)
                    if block is None:
                        raise ValueError(
                            "Anthropic content block stopped before its start"
                        )
                    if isinstance(block, _ToolUseBlockState):
                        arguments = "".join(block.argument_fragments)
                        block.arguments = (
                            _parse_tool_args(arguments, tool_name=block.name)
                            if arguments
                            else block.initial_input
                        )
                        yield ToolCallDelta(
                            call_id=block.call_id,
                            name_delta=block.name,
                            arguments_delta=arguments,
                        )
                elif event_type == "message_delta":
                    delta = event.delta
                    if delta.stop_reason is not None:
                        stop_reason = delta.stop_reason
                    _merge_anthropic_usage(usage_values, event.usage)
        finally:
            await stream.close()

        if usage_values.input_tokens is None:
            raise ValueError("Anthropic stream ended without message_start usage")
        input_tokens = usage_values.input_tokens
        output_tokens = usage_values.output_tokens
        stop: ModelStop
        if stop_reason == "tool_use":
            stop = ToolCallsRequestedStop()
        elif stop_reason in {"max_tokens", "length"}:
            stop = LengthLimitedStop(limit_kind="output_tokens")
        else:
            stop = CompletedStop()
        yield ModelCompleted(response=ModelResponse(
            parts=_response_parts(content_blocks),
            usage=UsageDelta(counters=TokenCounters(
                input=input_tokens,
                output=output_tokens,
                cache_read=usage_values.cache_read_input_tokens,
                cache_create=usage_values.cache_creation_input_tokens,
                prompt_cache_write=0,
            )),
            observed_context=ProviderMeasured(tokens=(
                input_tokens
                + usage_values.cache_read_input_tokens
                + usage_values.cache_creation_input_tokens
            )),
            stop=stop,
            provider_extensions=ProviderExtensions(
                provider="anthropic",
                payload={"model": response_model},
            ),
        ))


def anthropic_request_messages(
    messages: tuple[ProviderMessage, ...],
) -> tuple[str, list[dict[str, JsonValue]]]:
    system = "\n\n".join(
        part.text
        for message in messages
        if isinstance(message, ProviderSystem)
        for part in message.parts
        if part.text.strip()
    )
    return system, anthropic_messages(
        messages,
    )


def anthropic_messages(
    messages: tuple[ProviderMessage, ...],
) -> list[dict[str, JsonValue]]:
    result: list[dict[str, JsonValue]] = []
    for message in messages:
        if isinstance(message, ProviderSystem):
            continue
        blocks: list[dict[str, JsonValue]] = []
        if isinstance(message, ProviderTool):
            blocks.append({
                "type": "tool_result",
                "tool_use_id": message.call_id,
                "content": _parts_to_anthropic(message.parts),
            })
            target_role = "user"
        elif isinstance(message, ProviderAssistant):
            blocks.extend(_parts_to_anthropic(message.parts))
            target_role = "assistant"
        elif isinstance(message, ProviderUser):
            blocks.extend(_parts_to_anthropic(message.parts))
            target_role = "user"
        else:
            raise TypeError(f"Unsupported provider message: {type(message).__name__}")
        if not blocks:
            continue
        if result and result[-1]["role"] == target_role:
            result[-1]["content"].extend(blocks)
        else:
            result.append({"role": target_role, "content": blocks})
    return result


def _response_parts(
    blocks: dict[int, _AnthropicBlockState],
) -> list[CanonicalTextPart | CanonicalReasoningPart | ToolCall]:
    parts: list[CanonicalTextPart | CanonicalReasoningPart | ToolCall] = []
    for index in sorted(blocks):
        block = blocks[index]
        if isinstance(block, _TextBlockState):
            parts.append(CanonicalTextPart(text=block.text))
        elif isinstance(block, _ThinkingBlockState):
            provider_data = (
                ProviderExtensions(
                    provider="anthropic",
                    payload={"signature": block.signature},
                )
                if block.signature
                else None
            )
            parts.append(CanonicalReasoningPart(
                text=block.text,
                provider_extensions=provider_data,
            ))
        elif isinstance(block, _RedactedThinkingBlockState):
            parts.append(CanonicalReasoningPart(
                text="",
                provider_extensions=ProviderExtensions(
                    provider="anthropic",
                    payload={"redacted_data": block.data},
                ),
            ))
        else:
            if block.arguments is None:
                raise ValueError(
                    f"Anthropic tool block {index} ended without content_block_stop"
                )
            parts.append(ToolCall(
                id=block.call_id,
                name=block.name,
                args=block.arguments,
            ))
    return parts


def _parts_to_anthropic(
    parts: tuple,
) -> list[dict[str, JsonValue]]:
    blocks: list[dict[str, JsonValue]] = []
    for part in parts:
        if isinstance(part, CanonicalTextPart):
            blocks.append({"type": "text", "text": part.text})
        elif isinstance(part, ToolCall):
            blocks.append({
                "type": "tool_use",
                "id": part.id,
                "name": part.name,
                "input": part.args,
            })
        elif isinstance(part, ResolvedImagePart):
            if part.ref.media_type not in {
                "image/gif",
                "image/jpeg",
                "image/png",
                "image/webp",
            }:
                raise ValueError(
                    f"Unsupported Anthropic image type: {part.ref.media_type}"
                )
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": part.ref.media_type,
                    "data": resolved_image_data(part),
                },
            })
        elif isinstance(part, CanonicalReasoningPart):
            data = part.provider_extensions.payload if part.provider_extensions else {}
            if data.get("redacted_data"):
                blocks.append({
                    "type": "redacted_thinking",
                    "data": data["redacted_data"],
                })
            elif data.get("signature"):
                blocks.append({
                    "type": "thinking",
                    "thinking": part.text,
                    "signature": data["signature"],
                })
    return blocks


def anthropic_tool_schema(tool: ToolSchema) -> dict[str, JsonValue]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.parameters,
    }


def normalize_anthropic_usage(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int,
    cache_creation_input_tokens: int,
):
    return provider_usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        context_tokens=(
            input_tokens
            + cache_read_input_tokens
            + cache_creation_input_tokens
        ),
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
    )


def _merge_anthropic_usage(
    total: _UsageAccumulator,
    usage: Usage | MessageDeltaUsage,
) -> None:
    total.merge(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
    )


__all__ = ["AnthropicProvider"]


def create_anthropic_provider(provider_config, model_config):
    """Factory for the anthropic protocol route.

    ``provider_config`` is the adapter instance (endpoint + credentials);
    ``model_config`` is the selected specific model from its catalog.
    """
    protocol = provider_config.protocol
    logging.getLogger("xbotv2.llm").info(
        "creating anthropic provider=%s model=%s", protocol, model_config.model
    )
    return AnthropicProvider(
        **_provider_arguments(provider_config, model_config),
        extra_headers=provider_config.headers or None,
    )


__all__ = ["AnthropicProvider", "create_anthropic_provider"]
