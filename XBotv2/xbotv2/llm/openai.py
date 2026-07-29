"""OpenAI-compatible provider adapter."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from xbotv2.api.messages import ModelChunk, ModelResponse
from xbotv2.api.tools import ToolCall, ToolCallDelta
from xbotv2.llm.base import (
    BaseProvider,
    message_role,
    same_response_model,
    usage_metadata,
)

_OPENAI_MESSAGE = "openai_message"


class OpenAICompatibleProvider(BaseProvider):
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
        )
        kwargs: dict[str, Any] = {"api_key": api_key, "max_retries": 0}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = AsyncOpenAI(**kwargs)

    async def _astream_once(
        self,
        messages: list[Any],
        **_kwargs: Any,
    ) -> AsyncIterator[ModelChunk]:
        api_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": openai_messages(messages, model=self.model),
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
        reasoning_field = "reasoning_content"
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
                if reasoning:
                    reasoning_field = "reasoning"
            reasoning = reasoning or ""
            if reasoning:
                reasoning_parts.append(reasoning)
                yield ModelChunk(
                    content=reasoning,
                    additional_kwargs={"reasoning_content": reasoning},
                )
                continue

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
        native_message: dict[str, Any] = {
            "role": "assistant",
            "content": content or None,
        }
        if tool_calls:
            native_message["tool_calls"] = [
                openai_tool_call(tool_call) for tool_call in tool_calls
            ]
        if reasoning:
            native_message[reasoning_field] = reasoning
        additional_kwargs: dict[str, Any] = {
            _OPENAI_MESSAGE: native_message
        }
        if reasoning:
            additional_kwargs["reasoning_content"] = reasoning
        yield ModelResponse(
            content=content,
            tool_calls=tool_calls,
            response_metadata={
                "model_name": self.model,
                **({"stop_reason": stop_reason} if stop_reason else {}),
            },
            usage_metadata=final_usage,
            additional_kwargs=additional_kwargs,
        )


def openai_messages(
    messages: list[Any],
    *,
    model: str | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    system_parts = [
        str(getattr(message, "content", ""))
        for message in messages
        if message_role(message) == "system"
        and str(getattr(message, "content", "")).strip()
    ]
    if system_parts:
        result.append({"role": "system", "content": "\n\n".join(system_parts)})
    for message in messages:
        role = message_role(message)
        if role == "system":
            continue
        content = getattr(message, "content", "")
        if role == "tool":
            result.append(
                {
                    "role": "tool",
                    "content": str(content),
                    "tool_call_id": getattr(message, "tool_call_id", ""),
                }
            )
            continue
        native = (getattr(message, "additional_kwargs", {}) or {}).get(
            _OPENAI_MESSAGE
        )
        if (
            role == "assistant"
            and isinstance(native, dict)
            and same_response_model(message, model)
        ):
            result.append(dict(native))
            continue
        item: dict[str, Any] = {"role": role, "content": str(content)}
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            item["tool_calls"] = [
                openai_tool_call(tool_call) for tool_call in tool_calls
            ]
        result.append(item)
    return result


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
    reported_total = getattr(usage, "total_tokens", None)
    cache_read = getattr(usage, "prompt_cache_hit_tokens", None)
    if cache_read is None:
        cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_creation = getattr(usage, "prompt_cache_miss_tokens", None)
    if cache_creation is None:
        cache_creation = getattr(usage, "cache_creation_input_tokens", 0)
    return usage_metadata(
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        total_tokens=(
            int(reported_total) if reported_total is not None else None
        ),
        cache_read_input_tokens=int(cache_read or 0),
        cache_creation_input_tokens=int(cache_creation or 0),
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
