"""XBot-owned provider adapters."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from xbotv2.config.loader import expand_env
from xbotv2.llm.messages import XBotModelChunk, XBotModelResponse

logger = logging.getLogger("xbotv2.llm")


class OpenAICompatibleProvider:
    def __init__(
        self, *,
        model: str, api_key: str, base_url: str | None,
        temperature: float, max_tokens: int,
        reasoning_effort: str | None = None, thinking_enabled: bool = False,
    ):
        from openai import AsyncOpenAI

        self.model_name = model
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self.thinking_enabled = thinking_enabled
        self.bound_tools: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = AsyncOpenAI(**kwargs)

    def bind_tools(self, tools, **kwargs):
        clone = self.__class__(
            model=self.model, api_key=self.client.api_key,
            base_url=str(self.client.base_url) if self.client.base_url else None,
            temperature=self.temperature, max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort, thinking_enabled=self.thinking_enabled,
        )
        clone.bound_tools = list(tools)
        return clone

    async def astream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[XBotModelChunk]:
        api_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": provider_messages(messages),
            "tools": self.bound_tools or None,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.reasoning_effort:
            api_kwargs["reasoning_effort"] = self.reasoning_effort
        if self.thinking_enabled:
            api_kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        response = await self.client.chat.completions.create(**api_kwargs)

        reasoning_parts: list[str] = []
        content_parts: list[str] = []
        tool_call_buffers: dict[int, dict[str, Any]] = {}
        final_usage: dict[str, Any] = {}
        done = False

        async for chunk in response:
            if not chunk.choices:
                if getattr(chunk, "usage", None):
                    final_usage = openai_usage(chunk.usage)
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue

            # Reasoning content (DeepSeek R1 / thinking mode) — show first with header
            rc = getattr(delta, "reasoning_content", None) or ""
            if rc:
                if not reasoning_parts:
                    rc = "## Thinking\n\n" + rc
                reasoning_parts.append(rc)
                yield XBotModelChunk(content=rc, additional_kwargs={"reasoning_content": rc})
                continue

            # Regular content
            c = getattr(delta, "content", None) or ""
            if c:
                content_parts.append(c)
                yield XBotModelChunk(content=c)

            # Tool calls
            tc_list = getattr(delta, "tool_calls", None) or []
            for tc in tc_list:
                idx = getattr(tc, "index", 0)
                if idx not in tool_call_buffers:
                    tool_call_buffers[idx] = {"id": getattr(tc, "id", "") or "", "name": "", "args": ""}
                buf = tool_call_buffers[idx]
                if getattr(tc, "id", None):
                    buf["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn:
                    if getattr(fn, "name", None):
                        buf["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        buf["args"] += fn.arguments
                yield XBotModelChunk(
                    tool_call_chunks=[{
                        "index": idx,
                        "id": buf["id"],
                        "name": buf.get("name", ""),
                        "args": buf.get("args", ""),
                    }]
                )

            # Finish reason
            finish = getattr(chunk.choices[0], "finish_reason", None)
            if finish and finish != "tool_calls":
                done = True

        full_content = "".join(content_parts)
        full_reasoning = "".join(reasoning_parts)
        tool_calls = [
            {"name": b["name"], "args": _parse_tool_args(b["args"]), "id": b["id"], "type": "tool_call"}
            for b in tool_call_buffers.values() if b["name"]
        ]
        yield XBotModelResponse(
            content=full_content,
            tool_calls=tool_calls,
            response_metadata={"model_name": self.model},
            usage_metadata=final_usage,
            additional_kwargs={"reasoning_content": full_reasoning} if full_reasoning else {},
        )


class AnthropicProvider:
    def __init__(
        self, *,
        model: str, api_key: str, base_url: str | None,
        temperature: float, max_tokens: int,
        reasoning_effort: str | None = None, thinking_enabled: bool = False,
    ):
        from anthropic import AsyncAnthropic

        self.model_name = model
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self.thinking_enabled = thinking_enabled
        self.bound_tools: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = AsyncAnthropic(**kwargs)

    def bind_tools(self, tools, **kwargs):
        clone = self.__class__(
            model=self.model, api_key=self.client.api_key,
            base_url=getattr(self.client, "base_url", None),
            temperature=self.temperature, max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort, thinking_enabled=self.thinking_enabled,
        )
        clone.bound_tools = [anthropic_tool_schema(tool) for tool in tools]
        return clone

    async def astream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[XBotModelChunk]:
        api_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages(messages),
            "tools": self.bound_tools or None,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.reasoning_effort:
            api_kwargs["reasoning_effort"] = self.reasoning_effort
        if self.thinking_enabled:
            api_kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        response = await self.client.messages.create(**api_kwargs)
        content_parts = []
        tool_calls: list[dict[str, Any]] = []
        for block in response.content:
            if getattr(block, "type", "") == "text":
                content_parts.append(block.text)
            if getattr(block, "type", "") == "tool_use":
                tool_calls.append({"name": block.name, "args": block.input, "id": block.id, "type": "tool_call"})
        yield XBotModelChunk(
            content="".join(content_parts),
            tool_calls=tool_calls,
            response_metadata={"model_name": response.model},
            usage_metadata=anthropic_usage(getattr(response, "usage", None)),
        )


def create_llm(provider_config: Any) -> Any:
    provider, model, base_url, api_key, temperature, max_tokens, responses, reasoning_effort, thinking_enabled = provider_values(provider_config)
    api_key = expand_env(api_key) if api_key else ""
    base_url = expand_env(base_url) if base_url else None

    if provider == "mock":
        return create_mock_llm(responses)
    if provider in ("openai", "deepseek", "lmstudio-openai"):
        require_api_key(provider, model, api_key)
        logger.info("creating openai-compatible provider=%s model=%s", provider, model)
        return OpenAICompatibleProvider(
            model=model, api_key=api_key, base_url=base_url,
            temperature=temperature, max_tokens=max_tokens,
            reasoning_effort=reasoning_effort, thinking_enabled=thinking_enabled,
        )
    if provider in ("anthropic", "lmstudio"):
        require_api_key(provider, model, api_key)
        logger.info("creating anthropic provider=%s model=%s", provider, model)
        return AnthropicProvider(
            model=model, api_key=api_key, base_url=base_url,
            temperature=temperature, max_tokens=max_tokens,
            reasoning_effort=reasoning_effort, thinking_enabled=thinking_enabled,
        )
    raise ValueError(f"Unknown provider: {provider!r}")


def provider_values(provider_config: Any) -> tuple[str, str, str | None, str, float, int, list[dict[str, Any]], str | None, bool]:
    return (
        _get_cfg(provider_config, "provider", "openai"),
        _get_cfg(provider_config, "model", "gpt-4"),
        _get_cfg(provider_config, "base_url"),
        _get_cfg(provider_config, "api_key", ""),
        _get_cfg(provider_config, "temperature", 0.7),
        _get_cfg(provider_config, "max_tokens", 4096),
        _get_cfg(provider_config, "mock_responses", []),
        _get_cfg(provider_config, "reasoning_effort"),
        _get_cfg(provider_config, "thinking_enabled", False),
    )


def _get_cfg(provider_config: Any, key: str, default: Any = None) -> Any:
    if isinstance(provider_config, dict):
        return provider_config.get(key, default)
    return getattr(provider_config, key, default)


def require_api_key(provider: str, model: str, api_key: str) -> None:
    if not api_key:
        raise ValueError(
            f"Provider {provider!r} for model {model!r} requires api_key. "
            "Set the configured environment variable or providers.yaml api_key."
        )


def create_mock_llm(responses: list[dict[str, Any]]) -> Any:
    from xbotv2.llm.mock import MockLLM
    return MockLLM(responses=responses)


def provider_messages(messages: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        role = message_role(message)
        content = getattr(message, "content", "")
        if role == "tool":
            result.append({"role": "tool", "content": str(content), "tool_call_id": getattr(message, "tool_call_id", "")})
        else:
            item: dict[str, Any] = {"role": role, "content": str(content)}
            tool_calls = getattr(message, "tool_calls", None)
            if tool_calls:
                item["tool_calls"] = [openai_tool_call_for_request(tc) for tc in tool_calls]
                reasoning = (getattr(message, "additional_kwargs", {}) or {}).get("reasoning_content")
                if reasoning:
                    item["reasoning_content"] = reasoning
            result.append(item)
    return result


def message_role(message: Any) -> str:
    if hasattr(message, "role") and message.role:
        return message.role
    return "assistant"


def openai_tool_call_for_request(tool_call: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": tool_call.get("id", ""),
        "type": "function",
        "function": {
            "name": tool_call.get("name", ""),
            "arguments": json.dumps(tool_call.get("args", {}), ensure_ascii=False)
            if isinstance(tool_call.get("args"), dict)
            else str(tool_call.get("args", "{}")),
        },
    }


def openai_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for tc in tool_calls:
        args = tc.get("function", {}).get("arguments", "{}")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {}
        result.append({
            "name": tc.get("function", {}).get("name", tc.get("name", "")),
            "args": args,
            "id": tc.get("id", ""),
        })
    return result


def openai_usage(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    result = {
        "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "total_tokens": getattr(usage, "total_tokens", 0) or 0,
    }
    # DeepSeek disk cache: cached tokens are discounted
    hit = getattr(usage, "prompt_cache_hit_tokens", None) or getattr(usage, "cache_read_input_tokens", None)
    miss = getattr(usage, "prompt_cache_miss_tokens", None) or getattr(usage, "cache_creation_input_tokens", None)
    write = getattr(usage, "prompt_cache_write_tokens", None)
    if hit is not None:
        result["cache_read_input_tokens"] = int(hit)
    if miss is not None:
        result["cache_creation_input_tokens"] = int(miss)
    if write is not None:
        result["prompt_cache_write_tokens"] = int(write)
    return result


def _parse_tool_args(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, TypeError):
        return {}


def anthropic_messages(messages: list[Any]) -> list[dict[str, Any]]:
    result = []
    for msg in messages:
        role = getattr(msg, "role", "user")
        content = getattr(msg, "content", "")
        if role in ("system",):
            result.append({"role": "user", "content": [{"type": "text", "text": str(content)}]})
        elif role == "tool":
            result.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": getattr(msg, "tool_call_id", ""), "content": str(content)}],
            })
        elif role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": [{"type": "text", "text": str(content)}]}
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                item["content"] += [
                    {"type": "tool_use", "id": tc.get("id", ""), "name": tc.get("name", ""), "input": tc.get("args", {})}
                    for tc in tool_calls
                ]
            result.append(item)
        else:
            result.append({"role": "user", "content": str(content)})
    return result


def anthropic_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    fn = tool.get("function", tool)
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
    }


def anthropic_usage(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    return {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "total_tokens": (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0),
    }
