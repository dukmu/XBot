"""Shared contract for model provider adapters."""

from __future__ import annotations

import asyncio
import base64
import logging
from abc import ABC, abstractmethod
from copy import copy
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from xbotv2.api.messages import ModelChunk
from xbotv2.api.providers import InputModality, ProviderCapabilities
from xbotv2.api.prompts import prompt_container, prompt_element

logger = logging.getLogger("xbotv2.llm")


class ProviderRetryExhaustedError(RuntimeError):
    """A provider request failed after all configured retries were consumed."""

    def __init__(
        self,
        *,
        model: str,
        retries: int,
        last_error: Exception,
    ) -> None:
        self.model = model
        self.retries = retries
        self.last_error = last_error
        super().__init__(
            f"Provider request for {model!r} failed after {retries} retries: "
            f"{last_error}"
        )


def attachment_prompt(message: Any) -> str:
    """Render uploaded file references without embedding their bytes."""
    children = []
    for value in getattr(message, "artifact", None) or []:
        item = value.to_dict() if hasattr(value, "to_dict") else value
        if not isinstance(item, dict) or not item.get("id"):
            continue
        path = str(item["id"])
        if not path.startswith("session/"):
            path = f"session/{path}"
        children.append(prompt_element(
            "attachment",
            "Use filesystem or shell tools to inspect this file when needed.",
            attributes={
                "name": item.get("name") or Path(path).name,
                "media_type": item.get("media_type") or "application/octet-stream",
                "path": path,
                "size": item.get("size"),
            },
        ))
    return prompt_container("attachments", children) if children else ""


class BaseProvider(ABC):
    """Provider-neutral configuration and Tool binding behavior."""

    supported_input_modalities: frozenset[InputModality] = frozenset({"text"})

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
        input_modalities: list[InputModality] | None = None,
        media_root: Path | str | None = None,
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
        requested = frozenset(input_modalities or ["text"])
        unsupported = requested - self.supported_input_modalities
        if unsupported:
            raise ValueError(
                "Provider adapter does not support input modalities: "
                + ", ".join(sorted(unsupported))
            )
        self.capabilities = ProviderCapabilities(requested)
        self.media_root = Path(media_root).resolve() if media_root else None
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
        self._validate_message_capabilities(messages)
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
                if self.max_retries is not None and retries >= self.max_retries:
                    raise ProviderRetryExhaustedError(
                        model=self.model,
                        retries=retries,
                        last_error=exc,
                    ) from exc
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

    def read_image(self, path: str) -> str:
        """Read a session-relative media artifact as base64."""
        if self.media_root is None:
            raise ValueError("Provider media root is not configured")
        target = (self.media_root / path).resolve()
        media_dir = (self.media_root / "artifacts" / "media").resolve()
        if not target.is_relative_to(media_dir):
            raise ValueError("Image path is outside the session media store")
        return base64.b64encode(target.read_bytes()).decode("ascii")

    def _validate_message_capabilities(self, messages: list[Any]) -> None:
        image_messages = [
            message for message in messages
            if getattr(message, "images", None)
        ]
        if image_messages:
            if any(
                getattr(message, "role", "") not in {"user", "tool"}
                for message in image_messages
            ):
                raise ValueError("Image content is supported only in user or tool messages")
            capabilities = getattr(
                self,
                "capabilities",
                ProviderCapabilities(),
            )
            if not capabilities.supports("image"):
                raise ValueError(
                    f"Provider model {self.model!r} does not support image input"
                )

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
            input_tokens
            + cache_read_input_tokens
            + cache_creation_input_tokens
            + output_tokens
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


__all__ = ["BaseProvider", "ProviderRetryExhaustedError"]
