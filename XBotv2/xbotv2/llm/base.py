"""Shared contract for model provider adapters."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from copy import copy
from typing import Any, AsyncIterator

import httpx

from xbotv2.api.messages import ModelChunk

logger = logging.getLogger("xbotv2.llm")


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
        max_retries: int | None = None,
        retry_backoff_factor: float = 0.5,
    ) -> None:
        if max_retries is not None and max_retries < 0:
            raise ValueError("max_retries must be non-negative or None")
        if retry_backoff_factor < 0:
            raise ValueError("retry_backoff_factor must be non-negative")
        self.model_name = model
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.thinking_enabled = thinking_enabled
        self.max_retries = max_retries
        self.retry_backoff_factor = retry_backoff_factor
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

    async def astream(
        self,
        messages: list[Any],
        **kwargs: Any,
    ) -> AsyncIterator[ModelChunk]:
        """Retry transient failures until output begins or the limit is reached."""
        retries = 0
        while True:
            emitted = False
            try:
                async for chunk in self._astream_once(messages, **kwargs):
                    emitted = True
                    yield chunk
                return
            except Exception as exc:
                if emitted or not retryable_provider_error(exc):
                    raise
                if (
                    self.max_retries is not None
                    and retries >= self.max_retries
                ):
                    raise
                delay = self.retry_backoff_factor * (2**retries)
                retries += 1
                logger.warning(
                    "provider request failed; retrying model=%s retry=%d "
                    "delay=%.1fs error=%s",
                    self.model,
                    retries,
                    delay,
                    exc,
                )
                if delay:
                    await asyncio.sleep(delay)

    @abstractmethod
    def _astream_once(
        self,
        messages: list[Any],
        **kwargs: Any,
    ) -> AsyncIterator[ModelChunk]:
        """Perform one provider request."""
        raise NotImplementedError


def retryable_provider_error(error: Exception) -> bool:
    """Return whether a provider transport failure is safe to retry."""
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        return status_code in {408, 409, 429} or status_code >= 500
    return isinstance(
        error,
        (ConnectionError, TimeoutError, httpx.TransportError),
    ) or type(error).__name__ in {
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
    }


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
