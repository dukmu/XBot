"""XBot-owned provider adapters."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from xbotv2.config.loader import expand_env
from xbotv2.api.messages import ModelChunk, ModelResponse
from xbotv2.api.tools import ToolCall, ToolCallDelta

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

    async def astream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[ModelChunk]:
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
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                final_usage = openai_usage(usage)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue

            # Reasoning content (DeepSeek R1 / thinking mode). The
            # provider yields the raw reasoning text verbatim; the
            # TUI renders it as part of the assistant message bubble.
            # We do NOT inject a `## Thinking` header here — injecting
            # one would compound each turn the reasoning is replayed
            # to the model and produce `## Thinking\n\n## Thinking\n\n…`
            # chains (see XBotv2/data/sessions/20260609-170727-7449).
            rc = getattr(delta, "reasoning_content", None) or ""
            if rc:
                reasoning_parts.append(rc)
                yield ModelChunk(content=rc, additional_kwargs={"reasoning_content": rc})
                continue

            # Regular content
            c = getattr(delta, "content", None) or ""
            if c:
                content_parts.append(c)
                yield ModelChunk(content=c)

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
                yield ModelChunk(
                    tool_call_chunks=[ToolCallDelta(
                        index=idx,
                        id=buf["id"],
                        name=buf.get("name", ""),
                        args=buf.get("args", ""),
                    )]
                )

            # Finish reason
            finish = getattr(chunk.choices[0], "finish_reason", None)
            if finish and finish != "tool_calls":
                done = True

        full_content = "".join(content_parts)
        full_reasoning = "".join(reasoning_parts)
        tool_calls = [
            ToolCall(id=b["id"], name=b["name"], args=_parse_tool_args(b["args"]))
            for b in tool_call_buffers.values() if b["name"]
        ]
        yield ModelResponse(
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

    async def astream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[ModelChunk]:
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

        # Track per-block state for tool_use blocks — the SDK
        # streams the JSON in pieces and we yield the parsed
        # args once the block ends.
        tool_blocks: dict[int, dict[str, Any]] = {}
        tool_json: dict[int, list[str]] = {}
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        final_usage: dict[str, Any] = {}

        async with self.client.messages.stream(**api_kwargs) as stream:
            async for event in stream:
                event_type = getattr(event, "type", "")
                if event_type == "content_block_start":
                    block = getattr(event, "content_block", None)
                    idx = int(getattr(event, "index", 0))
                    if getattr(block, "type", "") == "tool_use":
                        tool_blocks[idx] = {
                            "id": getattr(block, "id", ""),
                            "name": getattr(block, "name", ""),
                        }
                        tool_json[idx] = []
                elif event_type == "content_block_delta":
                    idx = int(getattr(event, "index", 0))
                    delta = getattr(event, "delta", None)
                    if delta is None:
                        continue
                    if getattr(delta, "type", "") == "input_json_delta":
                        partial = getattr(delta, "partial_json", "")
                        if partial:
                            tool_json.setdefault(idx, []).append(partial)
                elif event_type == "content_block_stop":
                    idx = int(getattr(event, "index", 0))
                    meta = tool_blocks.get(idx)
                    if meta is not None:
                        raw = "".join(tool_json.get(idx, []))
                        try:
                            args = json.loads(raw) if raw else {}
                        except json.JSONDecodeError:
                            args = {}
                        yield ModelChunk(
                            tool_calls=[ToolCall(
                                id=meta.get("id", ""),
                                name=meta.get("name", ""),
                                args=args,
                            )]
                        )
                elif event_type == "text":
                    text = getattr(event, "text", "")
                    if text:
                        text_parts.append(text)
                        yield ModelChunk(content=text)
                elif event_type == "thinking":
                    thinking = getattr(event, "thinking", "")
                    if thinking:
                        reasoning_parts.append(thinking)
                        yield ModelChunk(
                            content=thinking,
                            additional_kwargs={"reasoning_content": thinking},
                        )
                elif event_type == "message_delta":
                    usage = getattr(event, "usage", None)
                    if usage is not None:
                        final_usage = anthropic_usage(usage)

            final_message = await stream.get_final_message()
            # Reconstruct the full text + tool calls from the final
            # message so the engine sees the same shape as the
            # OpenAI provider: complete content + complete tool calls
            # in a single ModelResponse.
            final_text = "".join(text_parts)
            final_tool_calls: list[ToolCall] = []
            for block in getattr(final_message, "content", []) or []:
                btype = getattr(block, "type", "")
                if btype == "tool_use":
                    final_tool_calls.append(ToolCall(
                        id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                        args=getattr(block, "input", {}) or {},
                    ))
            yield ModelResponse(
                content=final_text,
                tool_calls=final_tool_calls,
                response_metadata={"model_name": getattr(final_message, "model", self.model)},
                usage_metadata=final_usage,
                additional_kwargs={"reasoning_content": "".join(reasoning_parts)} if reasoning_parts else {},
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


_REASONING_HEADER = "## Thinking\n\n"


def _strip_reasoning_headers(text: str) -> str:
    """Collapse leading ``## Thinking\n\n`` repetitions.

    Older sessions persisted multiple ``## Thinking`` headers (one
    per round-trip) when reasoning was re-emitted to the model. The
    chain would compound each turn, producing
    ``## Thinking\n\n## Thinking\n\n…`` with 4-20 nested headers
    after a few rounds. Strip them so the model sees a single clean
    block, and any pre-existing chain collapses to one.
    """

    if not text:
        return text
    changed = True
    while changed and text.startswith(_REASONING_HEADER):
        text = text[len(_REASONING_HEADER):]
    return text


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
                    item["reasoning_content"] = _strip_reasoning_headers(str(reasoning))
            result.append(item)
    return result


def message_role(message: Any) -> str:
    if hasattr(message, "role") and message.role:
        return message.role
    return "assistant"


def openai_tool_call_for_request(tool_call: ToolCall) -> dict[str, Any]:
    return {
        "id": tool_call.id,
        "type": "function",
        "function": {
            "name": tool_call.name,
            "arguments": json.dumps(tool_call.args, ensure_ascii=False),
        },
    }


def openai_tool_calls(tool_calls: list[dict[str, Any]]) -> list[ToolCall]:
    result: list[ToolCall] = []
    for tc in tool_calls:
        args = tc.get("function", {}).get("arguments", "{}")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {}
        result.append(ToolCall(
            id=tc.get("id", ""),
            name=tc.get("function", {}).get("name", tc.get("name", "")),
            args=args,
        ))
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
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args}
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
