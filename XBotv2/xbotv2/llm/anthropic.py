"""Anthropic provider adapter."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from xbotv2.api.messages import ModelChunk, ModelResponse
from xbotv2.api.tools import ToolCall
from xbotv2.llm.base import BaseProvider, same_response_model, usage_metadata

_ANTHROPIC_CONTENT = "anthropic_content"


class AnthropicProvider(BaseProvider):
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
    ) -> None:
        from anthropic import AsyncAnthropic

        super().__init__(
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            thinking_enabled=thinking_enabled,
        )
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = AsyncAnthropic(**kwargs)

    def _provider_tools(
        self,
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [anthropic_tool_schema(tool) for tool in tools]

    async def astream(
        self,
        messages: list[Any],
        **_kwargs: Any,
    ) -> AsyncIterator[ModelChunk]:
        system, request_messages = anthropic_request_messages(
            messages,
            model=self.model,
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
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
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
                            text_parts.append(text)
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
                            reasoning_parts.append(thinking)
                            yield ModelChunk(
                                content=thinking,
                                additional_kwargs={
                                    "reasoning_content": thinking
                                },
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
                            text_parts.append(text)
                            yield ModelChunk(content=text)
                    elif delta_type == "thinking_delta":
                        thinking = getattr(delta, "thinking", "")
                        if thinking:
                            content_blocks.setdefault(
                                index,
                                {"type": "thinking", "thinking": ""},
                            )["thinking"] += thinking
                            reasoning_parts.append(thinking)
                            yield ModelChunk(
                                content=thinking,
                                additional_kwargs={
                                    "reasoning_content": thinking
                                },
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

        tool_calls = [
            ToolCall(
                id=str(metadata.get("id") or ""),
                name=str(metadata.get("name") or ""),
                args=dict(metadata.get("input") or {}),
            )
            for metadata in tool_blocks.values()
            if metadata.get("name")
        ]
        response_metadata = {"model_name": response_model}
        if stop_reason:
            response_metadata["stop_reason"] = stop_reason
        additional_kwargs: dict[str, Any] = {
            _ANTHROPIC_CONTENT: [
                content_blocks[index] for index in sorted(content_blocks)
            ]
        }
        if reasoning_parts:
            additional_kwargs["reasoning_content"] = "".join(reasoning_parts)
        yield ModelResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            response_metadata=response_metadata,
            usage_metadata=normalize_anthropic_usage(**usage_values),
            additional_kwargs=additional_kwargs,
        )


def anthropic_request_messages(
    messages: list[Any],
    *,
    model: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    system = "\n\n".join(
        str(getattr(message, "content", ""))
        for message in messages
        if getattr(message, "role", "") == "system"
        and str(getattr(message, "content", "")).strip()
    )
    return system, anthropic_messages(messages, model=model)


def anthropic_messages(
    messages: list[Any],
    *,
    model: str | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        role = getattr(message, "role", "user")
        if role == "system":
            continue
        content = str(getattr(message, "content", "") or "")
        blocks: list[dict[str, Any]] = []
        target_role = "assistant" if role == "assistant" else "user"
        if role == "tool":
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": getattr(message, "tool_call_id", ""),
                "content": content,
            }
            if (getattr(message, "status", "") or "success") != "success":
                block["is_error"] = True
            blocks.append(block)
        elif role == "assistant":
            native = (
                getattr(message, "additional_kwargs", {}) or {}
            ).get(_ANTHROPIC_CONTENT)
            if isinstance(native, list) and same_response_model(message, model):
                blocks.extend(dict(block) for block in native)
            else:
                if content:
                    blocks.append({"type": "text", "text": content})
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "input": tool_call.args,
                    }
                    for tool_call in getattr(message, "tool_calls", None) or []
                )
        elif content:
            blocks.append({"type": "text", "text": content})
        if not blocks:
            continue
        if result and result[-1]["role"] == target_role:
            result[-1]["content"].extend(blocks)
        else:
            result.append({"role": target_role, "content": blocks})
    return result


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
