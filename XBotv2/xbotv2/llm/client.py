"""XBot-owned provider adapters."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from xbotv2.config.loader import expand_env
from xbotv2.api.messages import ModelChunk, ModelResponse
from xbotv2.api.tools import ToolCall, ToolCallDelta

logger = logging.getLogger("xbotv2.llm")

_ANTHROPIC_CONTENT = "anthropic_content"
_OPENAI_MESSAGE = "openai_message"


class OpenAICompatibleProvider:
    def __init__(
        self, *,
        model: str, api_key: str, base_url: str | None,
        temperature: float, max_output_tokens: int | None,
        reasoning_effort: str | None = None, thinking_enabled: bool = False,
    ):
        from openai import AsyncOpenAI

        self.model_name = model
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
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
            temperature=self.temperature, max_output_tokens=self.max_output_tokens,
            reasoning_effort=self.reasoning_effort, thinking_enabled=self.thinking_enabled,
        )
        clone.bound_tools = list(tools)
        return clone

    async def astream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[ModelChunk]:
        api_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": provider_messages(messages, model=self.model),
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
        final_usage: dict[str, Any] = {}
        stop_reason = ""

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
            rc = getattr(delta, "reasoning_content", None)
            if not rc:
                rc = getattr(delta, "reasoning", None)
                if rc:
                    reasoning_field = "reasoning"
            rc = rc or ""
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
                    tool_call_buffers[idx] = {
                        "id": getattr(tc, "id", "") or "",
                        "name": "",
                        "args": "",
                    }
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
            if finish:
                stop_reason = str(finish)

        full_content = "".join(content_parts)
        full_reasoning = "".join(reasoning_parts)
        tool_calls = [
            ToolCall(id=b["id"], name=b["name"], args=_parse_tool_args(b["args"]))
            for b in tool_call_buffers.values() if b["name"]
        ]
        openai_message: dict[str, Any] = {
            "role": "assistant",
            "content": full_content or None,
        }
        if tool_calls:
            openai_message["tool_calls"] = [
                openai_tool_call_for_request(call) for call in tool_calls
            ]
        if full_reasoning:
            openai_message[reasoning_field] = full_reasoning
        additional_kwargs: dict[str, Any] = {_OPENAI_MESSAGE: openai_message}
        if full_reasoning:
            additional_kwargs["reasoning_content"] = full_reasoning
        yield ModelResponse(
            content=full_content,
            tool_calls=tool_calls,
            response_metadata={
                "model_name": self.model,
                **({"stop_reason": stop_reason} if stop_reason else {}),
            },
            usage_metadata=final_usage,
            additional_kwargs=additional_kwargs,
        )


class AnthropicProvider:
    def __init__(
        self, *,
        model: str, api_key: str, base_url: str | None,
        temperature: float, max_output_tokens: int,
        reasoning_effort: str | None = None, thinking_enabled: bool = False,
    ):
        from anthropic import AsyncAnthropic

        self.model_name = model
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
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
            temperature=self.temperature, max_output_tokens=self.max_output_tokens,
            reasoning_effort=self.reasoning_effort, thinking_enabled=self.thinking_enabled,
        )
        clone.bound_tools = [anthropic_tool_schema(tool) for tool in tools]
        return clone

    async def astream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[ModelChunk]:
        system, request_messages = anthropic_request_messages(
            messages, model=self.model
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

        # Track per-block state for tool_use blocks — the SDK
        # streams the JSON in pieces and we yield the parsed
        # args once the block ends.
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
                    _merge_anthropic_usage(usage_values, getattr(message, "usage", None))
                elif event_type == "content_block_start":
                    block = getattr(event, "content_block", None)
                    idx = int(getattr(event, "index", 0))
                    block_type = getattr(block, "type", "")
                    if block_type == "tool_use":
                        tool_blocks[idx] = content_blocks[idx] = {
                            "type": "tool_use",
                            "id": getattr(block, "id", ""),
                            "name": getattr(block, "name", ""),
                            "input": {},
                        }
                        tool_json[idx] = []
                    elif block_type == "text":
                        text = str(getattr(block, "text", "") or "")
                        content_blocks[idx] = {"type": "text", "text": text}
                        if text:
                            text_parts.append(text)
                            yield ModelChunk(content=text)
                    elif block_type == "thinking":
                        thinking = str(getattr(block, "thinking", "") or "")
                        content_blocks[idx] = {
                            "type": "thinking",
                            "thinking": thinking,
                        }
                        signature = str(getattr(block, "signature", "") or "")
                        if signature:
                            content_blocks[idx]["signature"] = signature
                        if thinking:
                            reasoning_parts.append(thinking)
                            yield ModelChunk(
                                content=thinking,
                                additional_kwargs={"reasoning_content": thinking},
                            )
                    elif block_type == "redacted_thinking":
                        content_blocks[idx] = {
                            "type": "redacted_thinking",
                            "data": str(getattr(block, "data", "") or ""),
                        }
                elif event_type == "content_block_delta":
                    idx = int(getattr(event, "index", 0))
                    delta = getattr(event, "delta", None)
                    if delta is None:
                        continue
                    delta_type = getattr(delta, "type", "")
                    if delta_type == "input_json_delta":
                        partial = getattr(delta, "partial_json", "")
                        if partial:
                            tool_json.setdefault(idx, []).append(partial)
                    elif delta_type == "text_delta":
                        text = getattr(delta, "text", "")
                        if text:
                            content_blocks.setdefault(
                                idx, {"type": "text", "text": ""}
                            )["text"] += text
                            text_parts.append(text)
                            yield ModelChunk(content=text)
                    elif delta_type == "thinking_delta":
                        thinking = getattr(delta, "thinking", "")
                        if thinking:
                            content_blocks.setdefault(
                                idx, {"type": "thinking", "thinking": ""}
                            )["thinking"] += thinking
                            reasoning_parts.append(thinking)
                            yield ModelChunk(
                                content=thinking,
                                additional_kwargs={"reasoning_content": thinking},
                            )
                    elif delta_type == "signature_delta":
                        signature = getattr(delta, "signature", "")
                        if signature:
                            thinking_block = content_blocks.setdefault(
                                idx, {"type": "thinking", "thinking": ""}
                            )
                            thinking_block["signature"] = (
                                str(thinking_block.get("signature") or "") + signature
                            )
                elif event_type == "content_block_stop":
                    idx = int(getattr(event, "index", 0))
                    meta = tool_blocks.get(idx)
                    if meta is not None:
                        raw = "".join(tool_json.get(idx, []))
                        try:
                            args = json.loads(raw) if raw else {}
                        except json.JSONDecodeError:
                            args = {}
                        meta["input"] = args
                        yield ModelChunk(
                            tool_calls=[ToolCall(
                                id=meta.get("id", ""),
                                name=meta.get("name", ""),
                                args=args,
                            )]
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

        final_usage = _anthropic_usage_values(**usage_values)
        # Reconstruct the final response so the engine sees the same shape as
        # the OpenAI provider: complete content and complete tool calls.
        final_tool_calls = [
            ToolCall(
                id=str(meta.get("id") or ""),
                name=str(meta.get("name") or ""),
                args=dict(meta.get("input") or {}),
            )
            for meta in tool_blocks.values()
            if meta.get("name")
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
            tool_calls=final_tool_calls,
            response_metadata=response_metadata,
            usage_metadata=final_usage,
            additional_kwargs=additional_kwargs,
        )


def create_llm(provider_config: Any) -> Any:
    provider = _get_cfg(provider_config, "provider", "openai")
    model = _get_cfg(provider_config, "model", "gpt-4")
    base_url = _get_cfg(provider_config, "base_url")
    api_key = _get_cfg(provider_config, "api_key", "")
    temperature = _get_cfg(provider_config, "temperature", 0.7)
    max_output_tokens = _get_cfg(provider_config, "max_output_tokens")
    reasoning_effort = _get_cfg(provider_config, "reasoning_effort")
    thinking_enabled = _get_cfg(provider_config, "thinking_enabled", False)
    api_key = expand_env(api_key) if api_key else ""
    base_url = expand_env(base_url) if base_url else None

    if provider == "mock":
        from xbotv2.llm.mock import MockLLM

        return MockLLM(responses=_get_cfg(provider_config, "mock_responses", []))
    if provider in ("openai", "deepseek", "lmstudio-openai"):
        require_api_key(provider, model, api_key)
        logger.info("creating openai-compatible provider=%s model=%s", provider, model)
        return OpenAICompatibleProvider(
            model=model, api_key=api_key, base_url=base_url,
            temperature=temperature, max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort, thinking_enabled=thinking_enabled,
        )
    if provider in ("anthropic", "lmstudio"):
        if max_output_tokens is None:
            raise ValueError("Anthropic providers require max_output_tokens")
        require_api_key(provider, model, api_key)
        logger.info("creating anthropic provider=%s model=%s", provider, model)
        return AnthropicProvider(
            model=model, api_key=api_key, base_url=base_url,
            temperature=temperature, max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort, thinking_enabled=thinking_enabled,
        )
    raise ValueError(f"Unknown provider: {provider!r}")


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


def provider_messages(
    messages: list[Any], *, model: str | None = None
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
            result.append({
                "role": "tool",
                "content": str(content),
                "tool_call_id": getattr(message, "tool_call_id", ""),
            })
        else:
            native = (getattr(message, "additional_kwargs", {}) or {}).get(
                _OPENAI_MESSAGE
            )
            if (
                role == "assistant"
                and isinstance(native, dict)
                and _same_response_model(message, model)
            ):
                result.append(dict(native))
                continue
            item: dict[str, Any] = {"role": role, "content": str(content)}
            tool_calls = getattr(message, "tool_calls", None)
            if tool_calls:
                item["tool_calls"] = [openai_tool_call_for_request(tc) for tc in tool_calls]
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


def openai_usage(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    result = {
        "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "total_tokens": getattr(usage, "total_tokens", 0) or 0,
        "context_tokens": getattr(usage, "prompt_tokens", 0) or 0,
    }
    # DeepSeek disk cache: cached tokens are discounted
    hit = getattr(usage, "prompt_cache_hit_tokens", None) or getattr(
        usage, "cache_read_input_tokens", None
    )
    miss = getattr(usage, "prompt_cache_miss_tokens", None) or getattr(
        usage, "cache_creation_input_tokens", None
    )
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


def anthropic_request_messages(
    messages: list[Any],
    *,
    model: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    system = "\n\n".join(
        str(getattr(message, "content", ""))
        for message in messages
        if message_role(message) == "system"
        and str(getattr(message, "content", "")).strip()
    )
    return system, anthropic_messages(messages, model=model)


def anthropic_messages(
    messages: list[Any], *, model: str | None = None
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for msg in messages:
        role = getattr(msg, "role", "user")
        if role == "system":
            continue
        content = str(getattr(msg, "content", "") or "")
        blocks: list[dict[str, Any]] = []
        target_role = "assistant" if role == "assistant" else "user"
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": getattr(msg, "tool_call_id", ""),
                "content": content,
            }
            if (getattr(msg, "status", "") or "success") != "success":
                block["is_error"] = True
            blocks.append(block)
        elif role == "assistant":
            native = (getattr(msg, "additional_kwargs", {}) or {}).get(
                _ANTHROPIC_CONTENT
            )
            if isinstance(native, list) and _same_response_model(msg, model):
                blocks.extend(dict(block) for block in native)
            else:
                if content:
                    blocks.append({"type": "text", "text": content})
                tool_calls = getattr(msg, "tool_calls", None)
                if tool_calls:
                    blocks.extend(
                        {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args}
                        for tc in tool_calls
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


def _same_response_model(message: Any, model: str | None) -> bool:
    if model is None:
        return True
    response_model = (getattr(message, "response_metadata", {}) or {}).get(
        "model_name"
    )
    return response_model == model


def anthropic_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    fn = tool.get("function", tool)
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
    }


def _merge_anthropic_usage(total: dict[str, int], usage: Any) -> None:
    if usage is None:
        return
    for key in total:
        value = getattr(usage, key, None)
        if value is not None:
            total[key] = int(value)


def _anthropic_usage_values(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int,
    cache_creation_input_tokens: int,
) -> dict[str, Any]:
    cache_read = cache_read_input_tokens
    cache_creation = cache_creation_input_tokens
    result = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "context_tokens": input_tokens + cache_read + cache_creation,
    }
    if cache_read:
        result["cache_read_input_tokens"] = cache_read
    if cache_creation:
        result["cache_creation_input_tokens"] = cache_creation
    return result
