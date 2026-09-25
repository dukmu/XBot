"""Deterministic provider used by local smoke runs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from pydantic import JsonValue

from XBotv2.core.domain import (
    CompletedStop,
    MeasurementUnavailable,
    ProviderError,
    ProviderExtensions,
    TokenCounters,
    UsageDelta,
)
from XBotv2.core.parts import ReasoningPart, TextPart
from XBotv2.core.provider import ModelRequest
from XBotv2.core.providers import BaseProvider, InputModality
from XBotv2.core.stream import ModelCompleted, ModelResponse, ModelStreamEvent, ReasoningDelta, TextDelta
from XBotv2.core.tools import ToolCall


@dataclass
class _MockState:
    call_count: int = 0
    request_history: list[ModelRequest] = field(default_factory=list)


class MockLLM(BaseProvider):
    supported_input_modalities = frozenset({"text", "image"})

    def __init__(self, responses: list[dict[str, JsonValue]] | None = None, *,
                 input_modalities: list[InputModality] | None = None) -> None:
        super().__init__(input_modalities=input_modalities)
        self.responses = responses or []
        self._state = _MockState()

    @property
    def call_count(self) -> int:
        return self._state.call_count

    @property
    def request_history(self) -> tuple[ModelRequest, ...]:
        return tuple(self._state.request_history)

    async def _astream_once(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        response = self.next_response()
        self._state.request_history.append(request)
        final = self.to_response(response)
        chunks = response.get("chunks")
        if isinstance(chunks, list) and chunks:
            delay_raw = response.get("chunk_delay_ms", 0)
            delay = float(delay_raw) / 1000 if isinstance(delay_raw, (int, float)) and delay_raw > 0 else 0
            for chunk in chunks:
                if delay:
                    await asyncio.sleep(delay)
                if isinstance(chunk, str):
                    yield TextDelta(text=chunk)
                elif isinstance(chunk, dict):
                    if chunk.get("reasoning"):
                        yield ReasoningDelta(text=str(chunk["reasoning"]))
                    if chunk.get("content"):
                        yield TextDelta(text=str(chunk["content"]))
            yield ModelCompleted(response=final)
            return
        yield ModelCompleted(response=final)

    def next_response(self) -> dict[str, JsonValue]:
        if self._state.call_count >= len(self.responses):
            raise RuntimeError(f"MockLLM exhausted after {len(self.responses)} responses")
        response = self.responses[self._state.call_count]
        self._state.call_count += 1
        return response

    def normalize_provider_error(self, error: Exception) -> ProviderError:
        return ProviderError(
            code=type(error).__name__,
            message=str(error) or type(error).__name__,
            retryable=False,
            category="provider",
        )

    def to_response(self, response: dict[str, JsonValue]) -> ModelResponse:
        parts: list[TextPart | ReasoningPart | ToolCall] = []
        if response.get("content"):
            parts.append(TextPart(text=str(response["content"])))
        if response.get("reasoning"):
            parts.append(ReasoningPart(text=str(response["reasoning"])))
        parts.extend(normalize_tool_calls(response.get("tool_calls") or []))
        usage = response.get("usage_metadata") or {}
        counters = TokenCounters(
            input=int(usage.get("input_tokens", 0) or 0),
            output=int(usage.get("output_tokens", 0) or 0),
            cache_read=int(usage.get("cache_read_input_tokens", 0) or 0),
            cache_create=int(usage.get("cache_creation_input_tokens", 0) or 0),
            prompt_cache_write=int(usage.get("prompt_cache_write_tokens", 0) or 0),
        )
        return ModelResponse(
            parts=tuple(parts),
            usage=UsageDelta(counters=counters),
            observed_context=MeasurementUnavailable(reason="mock provider"),
            stop=CompletedStop(),
            provider_extensions=ProviderExtensions(provider="mock", payload={}),
        )


def normalize_tool_calls(tool_calls: list[dict[str, JsonValue]]) -> list[ToolCall]:
    return [ToolCall(id=str(item.get("id") or f"call_{i}"), name=str(item.get("name") or ""),
                     args=dict(item.get("args") or {})) for i, item in enumerate(tool_calls)]


def create_mock_provider(provider_config, model_config):
    return MockLLM(responses=model_config.mock_responses,
                   input_modalities=model_config.input_modalities)


__all__ = ["MockLLM", "create_mock_provider"]
