"""Shared provider adapter validation and retry execution."""
from __future__ import annotations
import asyncio
import logging
from abc import ABC, abstractmethod
from contextlib import aclosing
from typing import AsyncIterator
from XBotv2.core.api.domain import ProviderError
from XBotv2.core.api.provider import ModelRequest, ProviderUser, ProviderTool, ResolvedMediaPart
from XBotv2.core.api.providers import InputModality, ProviderCapabilities, ProviderFailure
from XBotv2.core.api.stream import ModelCancelled, ModelCompleted, ModelFailed, ModelStreamEvent
logger = logging.getLogger("xbotv2.llm")

class BaseProvider(ABC):
    """Provider-neutral configuration and Tool binding behavior."""

    supported_input_modalities: frozenset[InputModality] = frozenset({"text"})

    def __init__(
        self,
        *,
        max_retries: int | None = None,
        retry_backoff_factor: float = 0.5,
        request_timeout_seconds: float | None = 60.0,
        input_modalities: list[InputModality] | None = None,
    ) -> None:
        if max_retries is not None and max_retries < 0:
            raise ValueError("max_retries must be non-negative or None")
        if retry_backoff_factor < 0:
            raise ValueError("retry_backoff_factor must be non-negative")
        if request_timeout_seconds is not None and request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive or None")
        self.max_retries = max_retries
        self.retry_backoff_factor = retry_backoff_factor
        self.request_timeout_seconds = request_timeout_seconds
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
        """Stream provider output through the shared retry policy.

        Adapters configure transport timeouts; a live stream has no total deadline.
        """
        self._validate_message_capabilities(request)
        async with aclosing(self._astream_with_retries(request)) as stream:
            async for event in stream:
                yield event

    async def _astream_with_retries(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        """Retry transient failures until output begins or the limit is reached."""
        model = request.selection.route.model
        retries = 0
        while True:
            emitted = False
            terminal_seen = False
            try:
                async with aclosing(self._astream_once(request)) as stream:
                    async for chunk in stream:
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
        for message in request.messages:
            if not isinstance(message, (ProviderUser, ProviderTool)):
                continue
            for part in message.parts:
                if not isinstance(part, ResolvedMediaPart):
                    continue
                modality = part.artifact.media_type.partition("/")[0].lower()
                if modality not in ("image", "audio", "video"):
                    raise ValueError(f"Unsupported media type: {part.artifact.media_type}")
                if not self.capabilities.supports(modality):
                    raise ValueError(
                        f"Provider model {request.selection.route.model!r} "
                        f"does not support {modality} input"
                    )

    @abstractmethod
    def _astream_once(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        """Perform one provider request."""
        raise NotImplementedError
