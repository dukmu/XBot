"""Anthropic provider adapter."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Callable

from XBotv2.core.messages import (
    ContentPart,
    Message,
    ImagePart,
    ModelChunk,
    ModelResponse,
    ReasoningPart,
    TextPart,
    ToolCallPart,
)
from XBotv2.core.tools import ToolCall
from XBotv2.core.providers import BaseProvider
from XBotv2.llm.base import attachment_prompt, usage_metadata

class AnthropicProvider(BaseProvider):
    supported_input_modalities = frozenset({"text", "image"})

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None,
        temperature: float,
        max_output_tokens: int,
        reasoning_effort: str | None = None,
        thinking_enabled: bool = False,
        max_retries: int | None = None,
        retry_backoff_factor: float = 0.5,
        input_modalities: list[str] | None = None,
        media_root: str | None = None,
    ) -> None:
        from anthropic import AsyncAnthropic

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
        self.client = AsyncAnthropic(**kwargs)

    def _provider_tools(
        self,
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [anthropic_tool_schema(tool) for tool in tools]

    async def _astream_once(
        self,
        messages: list[Message],
        **_kwargs: Any,
    ) -> AsyncIterator[ModelChunk]:
        system, request_messages = anthropic_request_messages(
            messages,
            image_loader=self.read_image,
        )
        api_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": request_messages,
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
        }
        if system:
            api_kwargs["system"] = system
        if self.bound_tools:
            api_kwargs["tools"] = self.bound_tools
        extra_body: dict[str, Any] = {}
        if self.reasoning_effort:
            extra_body["reasoning_effort"] = self.reasoning_effort
        if self.thinking_enabled:
            extra_body["thinking"] = {"type": "enabled"}
        if extra_body:
            api_kwargs["extra_body"] = extra_body

        tool_blocks: dict[int, dict[str, Any]] = {}
        tool_json: dict[int, list[str]] = {}
        content_blocks: dict[int, dict[str, Any]] = {}
        usage_values = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        response_model = self.model
        stop_reason = ""

        stream = await self.client.messages.create(stream=True, **api_kwargs)
        try:
            async for event in stream:
                event_type = getattr(event, "type", "")
                if event_type == "message_start":
                    message = getattr(event, "message", None)
                    response_model = getattr(message, "model", self.model)
                    _merge_anthropic_usage(
                        usage_values,
                        getattr(message, "usage", None),
                    )
                elif event_type == "content_block_start":
                    block = getattr(event, "content_block", None)
                    index = int(getattr(event, "index", 0))
                    block_type = getattr(block, "type", "")
                    if block_type == "tool_use":
                        tool_blocks[index] = content_blocks[index] = {
                            "type": "tool_use",
                            "id": getattr(block, "id", ""),
                            "name": getattr(block, "name", ""),
                            "input": {},
                        }
                        tool_json[index] = []
                    elif block_type == "text":
                        text = str(getattr(block, "text", "") or "")
                        content_blocks[index] = {"type": "text", "text": text}
                        if text:
                            yield ModelChunk(content=text)
                    elif block_type == "thinking":
                        thinking = str(getattr(block, "thinking", "") or "")
                        content_blocks[index] = {
                            "type": "thinking",
                            "thinking": thinking,
                        }
                        signature = str(getattr(block, "signature", "") or "")
                        if signature:
                            content_blocks[index]["signature"] = signature
                        if thinking:
                            yield ModelChunk(
                                reasoning=thinking,
                            )
                    elif block_type == "redacted_thinking":
                        content_blocks[index] = {
                            "type": "redacted_thinking",
                            "data": str(getattr(block, "data", "") or ""),
                        }
                elif event_type == "content_block_delta":
                    index = int(getattr(event, "index", 0))
                    delta = getattr(event, "delta", None)
                    if delta is None:
                        continue
                    delta_type = getattr(delta, "type", "")
                    if delta_type == "input_json_delta":
                        partial = getattr(delta, "partial_json", "")
                        if partial:
                            tool_json.setdefault(index, []).append(partial)
                    elif delta_type == "text_delta":
                        text = getattr(delta, "text", "")
                        if text:
                            content_blocks.setdefault(
                                index,
                                {"type": "text", "text": ""},
                            )["text"] += text
                            yield ModelChunk(content=text)
                    elif delta_type == "thinking_delta":
                        thinking = getattr(delta, "thinking", "")
                        if thinking:
                            content_blocks.setdefault(
                                index,
                                {"type": "thinking", "thinking": ""},
                            )["thinking"] += thinking
                            yield ModelChunk(
                                reasoning=thinking,
                            )
                    elif delta_type == "signature_delta":
                        signature = getattr(delta, "signature", "")
                        if signature:
                            thinking_block = content_blocks.setdefault(
                                index,
                                {"type": "thinking", "thinking": ""},
                            )
                            thinking_block["signature"] = (
                                str(thinking_block.get("signature") or "")
                                + signature
                            )
                elif event_type == "content_block_stop":
                    index = int(getattr(event, "index", 0))
                    metadata = tool_blocks.get(index)
                    if metadata is not None:
                        args = _parse_tool_args(
                            "".join(tool_json.get(index, []))
                        )
                        metadata["input"] = args
                        yield ModelChunk(
                            tool_calls=[
                                ToolCall(
                                    id=metadata.get("id", ""),
                                    name=metadata.get("name", ""),
                                    args=args,
                                )
                            ]
                        )
                elif event_type == "message_delta":
                    delta = getattr(event, "delta", None)
                    stop_reason = (
                        getattr(delta, "stop_reason", "") or stop_reason
                    )
                    _merge_anthropic_usage(
                        usage_values,
                        getattr(event, "usage", None),
                    )
        finally:
            await stream.close()

        response_metadata = {"model_name": response_model}
        if stop_reason:
            response_metadata["stop_reason"] = stop_reason
        yield ModelResponse(
            parts=_response_parts(content_blocks),
            response_metadata=response_metadata,
            usage_metadata=normalize_anthropic_usage(**usage_values),
        )


def anthropic_request_messages(
    messages: list[Message],
    *,
    image_loader: Callable[[str], str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    system = "\n\n".join(
        message.content
        for message in messages
        if message.role == "system" and message.content.strip()
    )
    return system, anthropic_messages(
        messages,
        image_loader=image_loader,
    )


def anthropic_messages(
    messages: list[Message],
    *,
    image_loader: Callable[[str], str] | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        role = message.role
        if role == "system":
            continue
        content = message.content
        blocks: list[dict[str, Any]] = []
        target_role = "assistant" if role == "assistant" else "user"
        if role == "tool":
            tool_content = _parts_to_anthropic(
                message.parts,
                image_loader=image_loader,
            )
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": tool_content if message.images else content,
            }
            if (message.status or "success") != "success":
                block["is_error"] = True
            blocks.append(block)
        elif role == "assistant":
            blocks.extend(_parts_to_anthropic(
                message.parts,
                image_loader=image_loader,
            ))
        else:
            blocks.extend(_parts_to_anthropic(
                message.parts,
                image_loader=image_loader,
            ))
            attachments = attachment_prompt(message)
            if attachments:
                blocks.append({"type": "text", "text": attachments})
        if not blocks:
            continue
        if result and result[-1]["role"] == target_role:
            result[-1]["content"].extend(blocks)
        else:
            result.append({"role": target_role, "content": blocks})
    return result


def _response_parts(blocks: dict[int, dict[str, Any]]) -> list[ContentPart]:
    parts: list[ContentPart] = []
    for block in (blocks[index] for index in sorted(blocks)):
        block_type = block.get("type")
        if block_type == "text":
            parts.append(TextPart(str(block.get("text") or "")))
        elif block_type == "thinking":
            provider_data = {}
            if block.get("signature"):
                provider_data = {
                    "anthropic": {"signature": block["signature"]}
                }
            parts.append(ReasoningPart(
                str(block.get("thinking") or ""),
                provider_data,
            ))
        elif block_type == "redacted_thinking":
            parts.append(ReasoningPart(
                "",
                {"anthropic": {"redacted_data": block.get("data", "")}},
            ))
        elif block_type == "tool_use":
            parts.append(ToolCallPart(ToolCall(
                id=str(block.get("id") or ""),
                name=str(block.get("name") or ""),
                args=dict(block.get("input") or {}),
            )))
    return parts


def _parts_to_anthropic(
    parts: list[ContentPart],
    *,
    image_loader: Callable[[str], str] | None,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, TextPart):
            blocks.append({"type": "text", "text": part.text})
        elif isinstance(part, ToolCallPart):
            blocks.append({
                "type": "tool_use",
                "id": part.call.id,
                "name": part.call.name,
                "input": part.call.args,
            })
        elif isinstance(part, ImagePart):
            if image_loader is None:
                raise ValueError("Image loader is required for image content")
            if part.image.media_type not in {
                "image/gif",
                "image/jpeg",
                "image/png",
                "image/webp",
            }:
                raise ValueError(
                    f"Unsupported Anthropic image type: {part.image.media_type}"
                )
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": part.image.media_type,
                    "data": image_loader(part.image.path),
                },
            })
        elif isinstance(part, ReasoningPart):
            data = part.provider_data.get("anthropic") or {}
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


def anthropic_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    function = tool.get("function", tool)
    return {
        "name": function.get("name", ""),
        "description": function.get("description", ""),
        "input_schema": function.get(
            "parameters",
            {"type": "object", "properties": {}},
        ),
    }


def normalize_anthropic_usage(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int,
    cache_creation_input_tokens: int,
) -> dict[str, int]:
    return usage_metadata(
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


def _merge_anthropic_usage(total: dict[str, int], usage: Any) -> None:
    if usage is None:
        return
    for key in total:
        value = getattr(usage, key, None)
        if value is not None:
            total[key] = int(value)


def _parse_tool_args(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


__all__ = ["AnthropicProvider"]


def create_anthropic_provider(provider_config, *, media_root=None):
    """Factory for the anthropic route (anthropic / lmstudio)."""
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
        "creating anthropic provider=%s model=%s", provider, provider_config.model
    )
    return AnthropicProvider(
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


__all__ = [*globals().get("__all__", []), "create_anthropic_provider"]
