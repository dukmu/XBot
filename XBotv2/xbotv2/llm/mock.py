"""XBot-owned deterministic provider for tests."""

from __future__ import annotations

from typing import Any, AsyncIterator

from xbotv2.llm.messages import XBotModelChunk, XBotModelResponse


class MockLLM:
    """Deterministic provider with the same public test helpers as the old mock."""

    def __init__(self, responses: list[dict[str, Any]] | None = None, **kwargs):
        self.responses = responses or []
        self.call_count = 0
        self.bound_tools: list[Any] = []
        self.call_history: list[dict[str, Any]] = []
        self._mock_call_history = self.call_history

    def bind_tools(self, tools, **kwargs):
        self.bound_tools = list(tools)
        return self

    def invoke(self, messages: list[Any], **kwargs: Any) -> XBotModelResponse:
        response = self.next_response()
        result = self.to_response(response)
        self.record_call(messages=messages, kwargs=kwargs, response=result, raw_response=response)
        return result

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> XBotModelResponse:
        return self.invoke(messages, **kwargs)

    async def astream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[XBotModelChunk]:
        response = self.next_response()
        result = self.to_response(response)
        self.record_call(messages=messages, kwargs=kwargs, response=result, raw_response=response)
        chunks = response.get("chunks")
        if isinstance(chunks, list) and chunks:
            for chunk in chunks:
                yield self.to_chunk(chunk)
            yield result
            return
        yield XBotModelChunk(
            content=result.content,
            tool_calls=result.tool_calls,
            response_metadata=result.response_metadata,
            usage_metadata=result.usage_metadata,
            additional_kwargs=result.additional_kwargs,
        )

    def get_call_messages(self, index: int) -> list[Any]:
        return self.call_history[index].get("messages", [])

    def verify_tool_call_made(self, tool_name: str, min_count: int = 1) -> bool:
        count = sum(
            1
            for call in self.call_history
            for tool_call in call.get("tool_calls", [])
            if tool_call.get("name") == tool_name
        )
        return count >= min_count

    def reset(self) -> None:
        self.call_count = 0
        self.call_history = []
        self._mock_call_history = self.call_history

    def set_responses(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.reset()

    def next_response(self) -> dict[str, Any]:
        if self.call_count >= len(self.responses):
            raise RuntimeError(
                f"MockLLM exhausted after {len(self.responses)} responses "
                f"(requested response #{self.call_count + 1})"
            )
        response = self.responses[self.call_count]
        self.call_count += 1
        return response

    def to_response(self, response: dict[str, Any]) -> XBotModelResponse:
        return XBotModelResponse(
            content=str(response.get("content", "")),
            tool_calls=normalize_tool_calls(response.get("tool_calls") or []),
            response_metadata=dict(response.get("response_metadata") or {}),
            usage_metadata=dict(response.get("usage_metadata") or {}),
            additional_kwargs=additional_kwargs(response),
        )

    def to_chunk(self, raw: Any) -> XBotModelChunk:
        if isinstance(raw, str):
            return XBotModelChunk(content=raw)
        if not isinstance(raw, dict):
            return XBotModelChunk(content=str(raw))
        return XBotModelChunk(
            content=str(raw.get("content", "")),
            tool_calls=normalize_tool_calls(raw.get("tool_calls") or []),
            tool_call_chunks=list(raw.get("tool_call_chunks") or []),
            response_metadata=dict(raw.get("response_metadata") or {}),
            usage_metadata=dict(raw.get("usage_metadata") or {}),
            additional_kwargs=additional_kwargs(raw),
        )

    def record_call(
        self,
        *,
        messages: list[Any],
        kwargs: dict[str, Any],
        response: XBotModelResponse,
        raw_response: dict[str, Any],
    ) -> None:
        self.call_history.append({
            "messages": list(messages),
            "kwargs": dict(kwargs),
            "response": response,
            "raw_response": raw_response,
            "tool_calls": list(response.tool_calls),
        })


def normalize_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        normalized.append({
            "name": tool_call["name"],
            "args": tool_call.get("args", {}),
            "id": tool_call.get("id", f"call_{len(normalized)}"),
            "type": "tool_call",
        })
    return normalized


def additional_kwargs(raw: dict[str, Any]) -> dict[str, Any]:
    kwargs = dict(raw.get("additional_kwargs") or {})
    reasoning = raw.get("reasoning") or raw.get("reasoning_content")
    if reasoning:
        kwargs["reasoning_content"] = str(reasoning)
    return kwargs
