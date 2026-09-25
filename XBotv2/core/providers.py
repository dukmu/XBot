"""Provider contracts: capability flags and the provider adapter contract.

Pure contracts — the concrete adapters (openai-compatible / anthropic /
mock) live in ``XBotv2.llm``, and the provider route service (``ctx.llm``)
is built by the llm plugin.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal

from XBotv2.core.domain import ProviderError
from XBotv2.core.provider import ModelRequest, ProviderUser, ProviderTool, ResolvedImagePart
from XBotv2.core.stream import (
    ModelCancelled,
    ModelCompleted,
    ModelFailed,
    ModelStreamEvent,
)

logger = logging.getLogger("xbotv2.llm")

InputModality = Literal["text", "image"]


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    input_modalities: frozenset[InputModality] = field(
        default_factory=lambda: frozenset({"text"})
    )

    def supports(self, modality: InputModality) -> bool:
        return modality in self.input_modalities


class ProviderFailure(RuntimeError):
    """Exception projection of a typed provider failure for hook boundaries."""

    def __init__(self, error: ProviderError) -> None:
        super().__init__(error.message)
        self.error = error


class BaseProvider(ABC):
    """Provider-neutral configuration and Tool binding behavior."""

    supported_input_modalities: frozenset[InputModality] = frozenset({"text"})

    def __init__(
        self,
        *,
        max_retries: int | None = None,
        retry_backoff_factor: float = 0.5,
        input_modalities: list[InputModality] | None = None,
    ) -> None:
        if max_retries is not None and max_retries < 0:
            raise ValueError("max_retries must be non-negative or None")
        if retry_backoff_factor < 0:
            raise ValueError("retry_backoff_factor must be non-negative")
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

    async def astream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        """Retry transient failures until output begins or the limit is reached."""
        self._validate_message_capabilities(request)
        model = request.selection.route.model
        retries = 0
        while True:
            emitted = False
            terminal_seen = False
            try:
                async for chunk in self._astream_once(request):
                    if terminal_seen:
                        raise ProviderFailure(ProviderError(
                            code="event_after_terminal",
                            message="Provider emitted an event after its terminal event",
                            retryable=False,
                            category="contract",
                        ))
                    emitted = True
                    terminal_seen = isinstance(
                        chunk,
                        (ModelCompleted, ModelFailed, ModelCancelled),
                    )
                    yield chunk
                if not terminal_seen:
                    yield ModelFailed(error=ProviderError(
                        code="missing_terminal",
                        message="Provider stream ended without a terminal event",
                        retryable=False,
                        category="contract",
                    ))
                return
            except ProviderFailure:
                raise
            except Exception as exc:
                normalized = self.normalize_provider_error(exc)
                if emitted or not normalized.retryable:
                    logger.error(
                        "provider.request.failed model=%s error_type=%s "
                        "error_code=%s error_category=%s emitted=%s "
                        "retryable=%s retries=%d",
                        model,
                        type(exc).__name__,
                        normalized.code,
                        normalized.category,
                        emitted,
                        normalized.retryable,
                        retries,
                    )
                    yield ModelFailed(error=normalized)
                    return
                if self.max_retries is not None and retries >= self.max_retries:
                    logger.error(
                        "provider.retry.exhausted model=%s error_type=%s "
                        "error_code=%s retries=%d",
                        model,
                        type(exc).__name__,
                        normalized.code,
                        retries,
                    )
                    yield ModelFailed(error=ProviderError(
                        code="retry_exhausted",
                        message=(
                            f"Provider request for {model!r} failed after "
                            f"{retries} retries: {normalized.message}"
                        ),
                        retryable=False,
                        category=normalized.category,
                        provider_details={
                            **normalized.provider_details,
                            "model": model,
                            "retries": retries,
                            "last_code": normalized.code,
                        },
                    ))
                    return
                delay = self.retry_backoff_factor * (2**retries)
                retries += 1
                logger.warning(
                    "provider request failed; retrying model=%s retry=%d "
                    "delay=%.1fs error_type=%s error_code=%s",
                    model,
                    retries,
                    delay,
                    type(exc).__name__,
                    normalized.code,
                )
                if delay:
                    await asyncio.sleep(delay)

    @abstractmethod
    def normalize_provider_error(self, error: Exception) -> ProviderError:
        """Normalize failures using this provider adapter's error contract."""
        raise NotImplementedError

    def _validate_message_capabilities(self, request: ModelRequest) -> None:
        image_messages = [
            message for message in request.messages
            if isinstance(message, (ProviderUser, ProviderTool))
            and any(isinstance(part, ResolvedImagePart) for part in message.parts)
        ]
        if image_messages:
            if not self.capabilities.supports("image"):
                raise ValueError(
                    f"Provider model {request.selection.route.model!r} "
                    "does not support image input"
                )

    @abstractmethod
    def _astream_once(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        """Perform one provider request."""
        raise NotImplementedError


__all__ = [
    "BaseProvider",
    "InputModality",
    "ProviderCapabilities",
    "ProviderFailure",
]
