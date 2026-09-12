"""Provider contracts: capability flags and the provider adapter contract.

Pure contracts — the concrete adapters (openai-compatible / anthropic /
mock) live in ``XBotv2.llm``, and the provider route service (``ctx.llm``)
is built by the llm plugin.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from copy import copy
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from XBotv2.core.artifacts import ArtifactStorePort
from XBotv2.core.messages import Message, ModelChunk

logger = logging.getLogger("xbotv2.llm")

InputModality = Literal["text", "image"]


class ModelRequestOptions(BaseModel):
    """Per-call overrides that a caller attaches to one model request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_output_tokens: int | None = Field(default=None, ge=1)


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    input_modalities: frozenset[InputModality] = field(
        default_factory=lambda: frozenset({"text"})
    )

    def supports(self, modality: InputModality) -> bool:
        return modality in self.input_modalities


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


class ProviderContextOverflowError(RuntimeError):
    """Provider-confirmed request context capacity failure."""


class BaseProvider(ABC):
    """Provider-neutral configuration and Tool binding behavior."""

    supported_input_modalities: frozenset[InputModality] = frozenset({"text"})

    def __init__(
        self,
        *,
        model: str,
        temperature: float | None,
        max_output_tokens: int | None,
        reasoning_effort: str | None = None,
        thinking: str | None = None,
        max_retries: int | None = None,
        retry_backoff_factor: float = 0.5,
        input_modalities: list[InputModality] | None = None,
        artifacts: ArtifactStorePort | None = None,
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
        self.thinking = thinking
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
        self.artifacts = artifacts
        self.bound_tools: list[dict[str, JsonValue]] = []

    def bind_tools(
        self,
        tools: list[dict[str, JsonValue]],
        **_kwargs: Any,
    ) -> BaseProvider:
        clone = copy(self)
        clone.bound_tools = self._provider_tools(tools)
        return clone

    def bind_artifacts(self, artifacts: ArtifactStorePort) -> BaseProvider:
        clone = copy(self)
        clone.artifacts = artifacts
        return clone

    def _provider_tools(
        self,
        tools: list[dict[str, JsonValue]],
    ) -> list[dict[str, JsonValue]]:
        return list(tools)

    async def astream(
        self,
        messages: list[Message],
        *,
        options: ModelRequestOptions | None = None,
    ) -> AsyncIterator[ModelChunk]:
        """Retry transient failures until output begins or the limit is reached."""
        self._validate_message_capabilities(messages)
        retries = 0
        while True:
            emitted = False
            try:
                async for chunk in self._astream_once(messages, options=options):
                    emitted = True
                    yield chunk
                return
            except Exception as exc:
                normalized = self.normalize_provider_error(exc)
                if normalized is not exc:
                    raise normalized from exc
                exc = normalized
                retryable = retryable_provider_error(exc)
                status_code = getattr(exc, "status_code", None)
                if emitted or not retryable:
                    logger.error(
                        "provider.request.failed model=%s error_type=%s "
                        "status_code=%s emitted=%s retryable=%s retries=%d",
                        self.model,
                        type(exc).__name__,
                        status_code,
                        emitted,
                        retryable,
                        retries,
                    )
                    raise
                if self.max_retries is not None and retries >= self.max_retries:
                    logger.error(
                        "provider.retry.exhausted model=%s error_type=%s "
                        "status_code=%s retries=%d",
                        self.model,
                        type(exc).__name__,
                        status_code,
                        retries,
                    )
                    raise ProviderRetryExhaustedError(
                        model=self.model,
                        retries=retries,
                        last_error=exc,
                    ) from exc
                delay = self.retry_backoff_factor * (2**retries)
                retries += 1
                logger.warning(
                    "provider request failed; retrying model=%s retry=%d "
                    "delay=%.1fs error_type=%s status_code=%s",
                    self.model,
                    retries,
                    delay,
                    type(exc).__name__,
                    status_code,
                )
                if delay:
                    await asyncio.sleep(delay)

    def normalize_provider_error(self, error: Exception) -> Exception:
        """Normalize adapter-specific SDK errors at the provider boundary."""
        return error

    def read_image(self, artifact_id: str) -> str:
        """Read a media artifact through the configured artifact service."""
        if self.artifacts is None:
            raise ValueError("Provider artifact storage is not configured")
        return base64.b64encode(self.artifacts.read(artifact_id)).decode("ascii")

    def _validate_message_capabilities(self, messages: list[Message]) -> None:
        image_messages = [
            message for message in messages if message.images
        ]
        if image_messages:
            if any(
                message.role not in {"user", "tool"}
                for message in image_messages
            ):
                raise ValueError("Image content is supported only in user or tool messages")
            if not self.capabilities.supports("image"):
                raise ValueError(
                    f"Provider model {self.model!r} does not support image input"
                )

    @abstractmethod
    def _astream_once(
        self,
        messages: list[Message],
        *,
        options: ModelRequestOptions | None = None,
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


def _provider_error_payload(error: object) -> Mapping[str, Any] | None:
    """Return the provider error object of an SDK exception, when present."""
    body = getattr(error, "body", None)
    if not isinstance(body, Mapping):
        return None
    nested = body.get("error")
    payload: Any = nested if isinstance(nested, Mapping) else body
    return payload if isinstance(payload, Mapping) else None


def provider_context_overflow(
    error: object,
    *,
    types: frozenset[str] = frozenset(),
    codes: frozenset[str] = frozenset(),
    statuses: frozenset[int] = frozenset(),
    message_prefixes: tuple[str, ...] = (),
) -> ProviderContextOverflowError | None:
    """Classify a provider error as context overflow from declared vocabulary.

    Each adapter declares only the documented discriminators of its own
    protocol; the classification order lives here so adapters stay declarative.
    """
    payload = _provider_error_payload(error)
    error_type = str(payload.get("type") or "") if payload else ""
    code = str(payload.get("code") or "") if payload else ""
    message = str(payload.get("message") or "") if payload else ""
    status_code = getattr(error, "status_code", None)
    if error_type and error_type in types:
        return ProviderContextOverflowError(message or error_type)
    if code and code in codes:
        return ProviderContextOverflowError(message or code)
    if isinstance(status_code, int) and status_code in statuses:
        return ProviderContextOverflowError(message or f"HTTP {status_code}")
    if (
        message_prefixes
        and status_code == 400
        and message.startswith(message_prefixes)
    ):
        return ProviderContextOverflowError(message)
    return None


__all__ = [
    "BaseProvider",
    "InputModality",
    "ModelRequestOptions",
    "ProviderCapabilities",
    "ProviderRetryExhaustedError",
    "ProviderContextOverflowError",
    "provider_context_overflow",
    "retryable_provider_error",
]
