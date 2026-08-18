"""XBot-owned deterministic provider for tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

from XBotv2.core.messages import Message, ModelChunk, ModelResponse
from XBotv2.core.providers import BaseProvider, InputModality
from XBotv2.core.tools import ToolCall, ToolCallDelta


class MockLLM(BaseProvider):
    """Deterministic streaming provider for tests."""

    supported_input_modalities = frozenset({"text", "image"})

    def __init__(
        self,
        responses: list[dict[str, Any]] | None = None,
        *,
        input_modalities: list[InputModality] | None = None,
        media_root: Path | str | None = None,
    ) -> None:
        super().__init__(
            model="mock",
            temperature=0,
            max_output_tokens=None,
            input_modalities=input_modalities,
            media_root=media_root,
        )
        self.responses = responses or []
        self.call_count = 0
        self.call_history: list[list[Message]] = []

    def bind_tools(
        self,
        tools: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> MockLLM:
        self.bound_tools = list(tools)
        return self

    async def _astream_once(
        self,
        messages: list[Message],
        **_kwargs: Any,
    ) -> AsyncIterator[ModelChunk]:
        response = self.next_response()
        result = self.to_response(response)
        self.call_history.append(list(messages))
        chunks = response.get("chunks")
        if isinstance(chunks, list) and chunks:
            for chunk in chunks:
                yield self.to_chunk(chunk)
            yield result
            return
        yield ModelChunk(
            content=result.content,
            reasoning=result.reasoning,
            tool_calls=result.tool_calls,
            response_metadata=result.response_metadata,
            usage_metadata=result.usage_metadata,
            additional_kwargs=result.additional_kwargs,
        )

    def get_call_messages(self, index: int) -> list[Message]:
        return self.call_history[index]

    def next_response(self) -> dict[str, Any]:
        if self.call_count >= len(self.responses):
            raise RuntimeError(
                f"MockLLM exhausted after {len(self.responses)} responses "
                f"(requested response #{self.call_count + 1})"
            )
        response = self.responses[self.call_count]
        self.call_count += 1
        return response

    def to_response(self, response: dict[str, Any]) -> ModelResponse:
        return ModelResponse(
            content=str(response.get("content", "")),
            reasoning=str(response.get("reasoning") or ""),
            tool_calls=normalize_tool_calls(response.get("tool_calls") or []),
            response_metadata=dict(response.get("response_metadata") or {}),
            usage_metadata=dict(response.get("usage_metadata") or {}),
            additional_kwargs=dict(response.get("additional_kwargs") or {}),
        )

    def to_chunk(self, raw: Any) -> ModelChunk:
        if isinstance(raw, str):
            return ModelChunk(content=raw)
        if not isinstance(raw, dict):
            return ModelChunk(content=str(raw))
        return ModelChunk(
            content=str(raw.get("content", "")),
            reasoning=str(raw.get("reasoning") or ""),
            tool_calls=normalize_tool_calls(raw.get("tool_calls") or []),
            tool_call_chunks=[
                ToolCallDelta(
                    index=int(chunk.get("index", 0)),
                    id=str(chunk.get("id") or ""),
                    name=str(chunk.get("name") or ""),
                    args=str(chunk.get("args") or ""),
                )
                for chunk in raw.get("tool_call_chunks") or []
            ],
            response_metadata=dict(raw.get("response_metadata") or {}),
            usage_metadata=dict(raw.get("usage_metadata") or {}),
            additional_kwargs=dict(raw.get("additional_kwargs") or {}),
        )


def normalize_tool_calls(tool_calls: list[dict[str, Any]]) -> list[ToolCall]:
    normalized: list[ToolCall] = []
    for tool_call in tool_calls:
        normalized.append(ToolCall.from_dict(
            tool_call,
            default_id=f"call_{len(normalized)}",
        ))
    return normalized


def create_mock_provider(provider_config, model_config, *, media_root=None):
    """Factory for the deterministic mock route."""
    return MockLLM(
        responses=model_config.mock_responses,
        input_modalities=model_config.input_modalities,
        media_root=media_root,
    )


__all__ = [*globals().get("__all__", []), "create_mock_provider"]
