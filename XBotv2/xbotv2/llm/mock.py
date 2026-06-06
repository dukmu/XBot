"""Mock LLM for deterministic testing.

Provides a BaseChatModel-compatible mock that returns pre-configured
response sequences. No real provider calls are made.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatResult, ChatGeneration, ChatGenerationChunk


class MockLLM(BaseChatModel):
    """Deterministic mock LLM for testing.

    Configure with a list of response dicts. Each dict is either:
    - {"content": str} — plain text response
    - {"content": str, "tool_calls": [...]} — response with tool calls

    Responses are consumed in order. Raises RuntimeError when exhausted.
    All mutable state is stored via ``object.__setattr__`` to avoid
    Pydantic field validation on BaseChatModel.
    """

    def __init__(self, responses: list[dict[str, Any]] | None = None, **kwargs):
        super().__init__(**kwargs)
        # Bypass Pydantic's __setattr__ validation
        object.__setattr__(self, "_mock_responses", responses or [])
        object.__setattr__(self, "_mock_idx", 0)
        object.__setattr__(self, "_mock_call_history", [])

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        response = self._next_response()
        msg = self._to_aimessage(response)
        self._record_call(
            messages=messages,
            stop=stop,
            kwargs=kwargs,
            response=msg,
            raw_response=response,
        )
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop, run_manager, **kwargs)

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        response = self._next_response()
        msg = self._to_aimessage(response)
        self._record_call(
            messages=messages,
            stop=stop,
            kwargs=kwargs,
            response=msg,
            raw_response=response,
        )
        for chunk in self._response_chunks(response, msg):
            yield chunk

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        result = await self._agenerate(messages, stop, run_manager, **kwargs)
        msg = result.generations[0].message
        raw_response = self._mock_call_history[-1].get("raw_response", {})
        for chunk in self._response_chunks(raw_response, msg):
            yield chunk

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    @property
    def responses(self) -> list[dict[str, Any]]:
        """The configured response list."""
        return self._mock_responses

    @property
    def call_count(self) -> int:
        """Number of times the LLM has been called."""
        return len(self._mock_call_history)

    def get_call_messages(self, index: int) -> list[BaseMessage]:
        """Return the messages sent for call *index*."""
        return self._mock_call_history[index].get("messages", [])

    def verify_tool_call_made(self, tool_name: str, min_count: int = 1) -> bool:
        """Check that *tool_name* was called at least *min_count* times."""
        count = sum(
            1
            for call in self._mock_call_history
            for tc in call.get("tool_calls", [])
            if tc.get("name") == tool_name
        )
        return count >= min_count

    def reset(self) -> None:
        """Reset the response index and call history."""
        object.__setattr__(self, "_mock_idx", 0)
        object.__setattr__(self, "_mock_call_history", [])

    def set_responses(self, responses: list[dict[str, Any]]) -> None:
        """Replace the response list (for test reuse)."""
        object.__setattr__(self, "_mock_responses", responses)
        self.reset()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _next_response(self) -> dict[str, Any]:
        idx = self._mock_idx
        responses = self._mock_responses
        if idx >= len(responses):
            raise RuntimeError(
                f"MockLLM exhausted after {len(responses)} responses "
                f"(requested response #{idx + 1})"
            )
        object.__setattr__(self, "_mock_idx", idx + 1)
        return responses[idx]

    def _to_aimessage(self, response: dict[str, Any]) -> AIMessage:
        content = response.get("content", "")
        tool_calls = response.get("tool_calls")
        metadata: dict[str, Any] = {}
        if isinstance(response.get("usage_metadata"), dict):
            metadata["usage_metadata"] = response["usage_metadata"]
        if isinstance(response.get("response_metadata"), dict):
            metadata["response_metadata"] = response["response_metadata"]

        if tool_calls:
            normalized = []
            for tc in tool_calls:
                normalized.append({
                    "name": tc["name"],
                    "args": tc.get("args", {}),
                    "id": tc.get("id", f"call_{len(normalized)}"),
                    "type": "tool_call",
                })
            msg = AIMessage(
                content=content,
                tool_calls=normalized,
                **metadata,
            )
        else:
            msg = AIMessage(content=content, **metadata)

        return msg

    def _response_chunks(
        self,
        response: dict[str, Any],
        msg: AIMessage,
    ) -> list[ChatGenerationChunk]:
        chunks = response.get("chunks")
        if isinstance(chunks, list) and chunks:
            return [
                ChatGenerationChunk(message=self._to_message_chunk(raw))
                for raw in chunks
            ]
        chunk_kwargs: dict[str, Any] = {
            "content": msg.content,
            "additional_kwargs": dict(getattr(msg, "additional_kwargs", {}) or {}),
            "response_metadata": dict(getattr(msg, "response_metadata", {}) or {}),
        }
        usage_metadata = getattr(msg, "usage_metadata", None)
        if usage_metadata is not None:
            chunk_kwargs["usage_metadata"] = usage_metadata
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            chunk_kwargs["tool_calls"] = tool_calls
        return [ChatGenerationChunk(message=AIMessageChunk(**chunk_kwargs))]

    def _to_message_chunk(self, raw: Any) -> AIMessageChunk:
        if isinstance(raw, str):
            return AIMessageChunk(content=raw)
        if not isinstance(raw, dict):
            return AIMessageChunk(content=str(raw))
        kwargs: dict[str, Any] = {"content": raw.get("content", "")}
        additional_kwargs = dict(raw.get("additional_kwargs") or {})
        reasoning = raw.get("reasoning") or raw.get("reasoning_content")
        if reasoning:
            additional_kwargs["reasoning_content"] = str(reasoning)
        if additional_kwargs:
            kwargs["additional_kwargs"] = additional_kwargs
        if isinstance(raw.get("response_metadata"), dict):
            kwargs["response_metadata"] = raw["response_metadata"]
        if isinstance(raw.get("usage_metadata"), dict):
            kwargs["usage_metadata"] = raw["usage_metadata"]
        if isinstance(raw.get("tool_call_chunks"), list):
            kwargs["tool_call_chunks"] = raw["tool_call_chunks"]
        if isinstance(raw.get("tool_calls"), list):
            kwargs["tool_calls"] = raw["tool_calls"]
        return AIMessageChunk(**kwargs)

    def _record_call(
        self,
        *,
        messages: list[BaseMessage],
        stop: list[str] | None,
        kwargs: dict[str, Any],
        response: AIMessage,
        raw_response: dict[str, Any],
    ) -> None:
        """Record one provider call for assertions in tests."""
        self._mock_call_history.append({
            "messages": list(messages),
            "stop": list(stop) if stop else None,
            "kwargs": dict(kwargs),
            "content": response.content,
            "tool_calls": getattr(response, "tool_calls", []) or [],
            "response": response,
            "raw_response": dict(raw_response),
        })

    @property
    def _llm_type(self) -> str:
        return "mock"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"mock_responses": len(self._mock_responses)}

    def bind_tools(
        self, tools: list[Any], **kwargs: Any
    ) -> "MockLLM":
        """Mock bind_tools — returns self since MockLLM handles tool calls
        in its pre-configured response data."""
        return self
