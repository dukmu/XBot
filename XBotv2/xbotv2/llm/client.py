"""XBot-owned provider adapters."""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from xbotv2.config.loader import expand_env
from xbotv2.llm.messages import XBotModelChunk, XBotModelResponse


logger = logging.getLogger("xbotv2.llm")


class OpenAICompatibleProvider:
    def __init__(self, *, model: str, api_key: str, base_url: str | None, temperature: float, max_tokens: int):
        from openai import AsyncOpenAI

        self.model_name = model
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.bound_tools: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = AsyncOpenAI(**kwargs)

    def bind_tools(self, tools, **kwargs):
        clone = self.__class__(
            model=self.model,
            api_key=self.client.api_key,
            base_url=str(self.client.base_url) if self.client.base_url else None,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        clone.bound_tools = list(tools)
        return clone

    async def astream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[XBotModelChunk]:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=provider_messages(messages),
            tools=self.bound_tools or None,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        choice = response.choices[0]
        message = choice.message
        yield XBotModelChunk(
            content=message.content or "",
            tool_calls=openai_tool_calls(getattr(message, "tool_calls", None) or []),
            response_metadata={"model_name": response.model},
            usage_metadata=openai_usage(getattr(response, "usage", None)),
        )


class AnthropicProvider:
    def __init__(self, *, model: str, api_key: str, base_url: str | None, temperature: float, max_tokens: int):
        from anthropic import AsyncAnthropic

        self.model = model
        self.model_name = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.bound_tools: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = AsyncAnthropic(**kwargs)

    def bind_tools(self, tools, **kwargs):
        clone = self.__class__(
            model=self.model,
            api_key=self.client.api_key,
            base_url=str(self.client.base_url) if getattr(self.client, "base_url", None) else None,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        clone.bound_tools = [anthropic_tool_schema(tool) for tool in tools]
        return clone

    async def astream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[XBotModelChunk]:
        response = await self.client.messages.create(
            model=self.model,
            messages=anthropic_messages(messages),
            tools=self.bound_tools or None,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
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
    provider, model, base_url, api_key, temperature, max_tokens, responses = provider_values(provider_config)
    api_key = expand_env(api_key) if api_key else ""
    base_url = expand_env(base_url) if base_url else None

    if provider == "mock":
        return create_mock_llm(responses)
    if provider in ("openai", "deepseek", "lmstudio-openai"):
        require_api_key(provider, model, api_key)
        logger.info("creating openai-compatible provider=%s model=%s base_url=%s", provider, model, base_url)
        return OpenAICompatibleProvider(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    if provider in ("anthropic", "lmstudio"):
        require_api_key(provider, model, api_key)
        logger.info("creating anthropic provider=%s model=%s base_url=%s", provider, model, base_url)
        return AnthropicProvider(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    raise ValueError(
        f"Unsupported LLM provider {provider!r}. "
        "Use one of: openai, deepseek, lmstudio-openai, anthropic, lmstudio, mock."
    )


def create_mock_llm(responses: list[dict[str, Any]] | None = None) -> Any:
    from xbotv2.llm.mock import MockLLM

    return MockLLM(responses=responses)


def provider_values(provider_config: Any) -> tuple[str, str, str | None, str, float, int, list[dict[str, Any]]]:
    if isinstance(provider_config, dict):
        return (
            provider_config.get("provider", "openai"),
            provider_config.get("model", "gpt-4"),
            provider_config.get("base_url"),
            provider_config.get("api_key", ""),
            provider_config.get("temperature", 0.7),
            provider_config.get("max_tokens", 4096),
            provider_config.get("mock_responses", []),
        )
    return (
        getattr(provider_config, "provider", "openai"),
        getattr(provider_config, "model", "gpt-4"),
        getattr(provider_config, "base_url", None),
        getattr(provider_config, "api_key", ""),
        getattr(provider_config, "temperature", 0.7),
        getattr(provider_config, "max_tokens", 4096),
        getattr(provider_config, "mock_responses", []),
    )


def require_api_key(provider: str, model: str, api_key: str) -> None:
    if not api_key:
        raise ValueError(
            f"Provider {provider!r} for model {model!r} requires api_key. "
            "Set the configured environment variable or providers.yaml api_key."
        )


def provider_messages(messages: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        role = message_role(message)
        content = getattr(message, "content", "")
        if role == "tool":
            result.append({"role": "tool", "content": str(content), "tool_call_id": getattr(message, "tool_call_id", "")})
        else:
            item = {"role": role, "content": str(content)}
            tool_calls = getattr(message, "tool_calls", None)
            if tool_calls:
                item["tool_calls"] = [openai_tool_call_for_request(tool_call) for tool_call in tool_calls]
            result.append(item)
    return result


def message_role(message: Any) -> str:
    if hasattr(message, "role") and message.role:
        return message.role
    return "assistant"


def openai_tool_call_for_request(tool_call: dict[str, Any]) -> dict[str, Any]:
    import json

    return {
        "id": tool_call.get("id", ""),
        "type": "function",
        "function": {
            "name": tool_call.get("name", ""),
            "arguments": json.dumps(tool_call.get("args", {}), ensure_ascii=False),
        },
    }


def openai_tool_calls(tool_calls: list[Any]) -> list[dict[str, Any]]:
    import json

    result: list[dict[str, Any]] = []
    for call in tool_calls:
        function = getattr(call, "function", None)
        args = getattr(function, "arguments", "{}") if function else "{}"
        result.append({
            "name": getattr(function, "name", "") if function else "",
            "args": json.loads(args or "{}"),
            "id": getattr(call, "id", f"call_{len(result)}"),
            "type": "tool_call",
        })
    return result


def openai_usage(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    return {
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
        "requests": 1,
    }


def anthropic_messages(messages: list[Any]) -> list[dict[str, Any]]:
    return [message for message in provider_messages(messages) if message["role"] != "system"]


def anthropic_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    function = tool.get("function", {}) if isinstance(tool, dict) else {}
    return {
        "name": function.get("name", ""),
        "description": function.get("description", ""),
        "input_schema": function.get("parameters", {"type": "object", "properties": {}}),
    }


def anthropic_usage(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "requests": 1,
    }
