"""Shared contract for model provider adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from copy import copy
from typing import Any, AsyncIterator

from xbotv2.api.messages import ModelChunk


class BaseProvider(ABC):
    """Provider-neutral configuration and Tool binding behavior."""

    def __init__(
        self,
        *,
        model: str,
        temperature: float,
        max_output_tokens: int | None,
        reasoning_effort: str | None = None,
        thinking_enabled: bool = False,
    ) -> None:
        self.model_name = model
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.thinking_enabled = thinking_enabled
        self.bound_tools: list[dict[str, Any]] = []

    def bind_tools(
        self,
        tools: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> BaseProvider:
        clone = copy(self)
        clone.bound_tools = self._provider_tools(tools)
        return clone

    def _provider_tools(
        self,
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return list(tools)

    @abstractmethod
    def astream(
        self,
        messages: list[Any],
        **kwargs: Any,
    ) -> AsyncIterator[ModelChunk]:
        """Stream normalized chunks followed by the complete response."""
        raise NotImplementedError


def usage_metadata(
    *,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int | None = None,
    context_tokens: int | None = None,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    prompt_cache_write_tokens: int = 0,
) -> dict[str, int]:
    """Build the normalized per-request usage contract consumed by Core."""

    result = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": (
            input_tokens + output_tokens
            if total_tokens is None
            else total_tokens
        ),
        "requests": 1,
        "context_tokens": (
            input_tokens if context_tokens is None else context_tokens
        ),
    }
    for key, value in (
        ("cache_read_input_tokens", cache_read_input_tokens),
        ("cache_creation_input_tokens", cache_creation_input_tokens),
        ("prompt_cache_write_tokens", prompt_cache_write_tokens),
    ):
        if value:
            result[key] = value
    return result


def message_role(message: Any) -> str:
    return str(getattr(message, "role", "") or "assistant")


def same_response_model(message: Any, model: str | None) -> bool:
    if model is None:
        return True
    response_model = (getattr(message, "response_metadata", {}) or {}).get(
        "model_name"
    )
    return response_model == model


__all__ = ["BaseProvider"]
