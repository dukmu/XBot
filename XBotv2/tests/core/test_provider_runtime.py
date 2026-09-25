"""Behavioral checks for provider retry and stream failure semantics."""

import pytest

from XBotv2.core.domain import (
    CompletedStop,
    GenerationSettings,
    MeasurementUnavailable,
    ModelRoute,
    ProviderExtensions,
    ResolvedModelSelection,
    StandardGenerationMode,
    TokenCounters,
    UsageDelta,
)
from XBotv2.core.parts import TextPart
from XBotv2.core.provider import ModelRequest, ProviderUser
from XBotv2.core.providers import BaseProvider
from XBotv2.llm.provider_errors import normalize_sdk_provider_error
from XBotv2.core.stream import (
    ModelCompleted,
    ModelFailed,
    ModelResponse,
    TextDelta,
)


def request() -> ModelRequest:
    return ModelRequest(
        messages=(ProviderUser(parts=(TextPart(text="hello"),)),),
        tools=(),
        selection=ResolvedModelSelection(
            route=ModelRoute(provider="test", model="test-model"),
            generation=GenerationSettings(
                mode=StandardGenerationMode(), max_output_tokens=32,
            ),
            context_window=1024,
        ),
    )


def completed(text: str = "answer") -> ModelCompleted:
    return ModelCompleted(response=ModelResponse(
        parts=(TextPart(text=text),),
        usage=UsageDelta(counters=TokenCounters()),
        observed_context=MeasurementUnavailable(reason="test provider"),
        stop=CompletedStop(),
        provider_extensions=ProviderExtensions(provider="test", payload={}),
    ))


class _RetryTestProvider(BaseProvider):
    def normalize_provider_error(self, error):
        return normalize_sdk_provider_error(error)


class TransientFailureThenSuccess(_RetryTestProvider):
    def __init__(self):
        super().__init__(max_retries=1, retry_backoff_factor=0)
        self.calls = 0

    async def _astream_once(self, _request):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("temporary disconnect")
        yield completed()


class PartialThenFailure(_RetryTestProvider):
    def __init__(self):
        super().__init__(max_retries=3, retry_backoff_factor=0)
        self.calls = 0

    async def _astream_once(self, _request):
        self.calls += 1
        yield TextDelta(text="partial")
        raise ConnectionError("disconnect after output")


class AlwaysTransientFailure(_RetryTestProvider):
    def __init__(self):
        super().__init__(max_retries=1, retry_backoff_factor=0)
        self.calls = 0

    async def _astream_once(self, _request):
        self.calls += 1
        raise ConnectionError("still unavailable")
        yield  # Make this an async generator.


@pytest.mark.asyncio
async def test_transient_failure_before_output_retries_and_completes():
    provider = TransientFailureThenSuccess()

    events = [event async for event in provider.astream(request())]

    assert provider.calls == 2
    assert len(events) == 1
    assert isinstance(events[0], ModelCompleted)
    assert events[0].response.parts == (TextPart(text="answer"),)


@pytest.mark.asyncio
async def test_failure_after_partial_output_is_not_retried():
    provider = PartialThenFailure()

    events = [event async for event in provider.astream(request())]

    assert provider.calls == 1
    assert isinstance(events[0], TextDelta)
    assert events[0].text == "partial"
    assert isinstance(events[-1], ModelFailed)
    assert events[-1].error.category == "transport"
    assert events[-1].error.retryable is True


@pytest.mark.asyncio
async def test_retry_exhaustion_is_reported_as_non_retryable_failure():
    provider = AlwaysTransientFailure()

    events = [event async for event in provider.astream(request())]

    assert provider.calls == 2
    assert len(events) == 1
    assert isinstance(events[0], ModelFailed)
    assert events[0].error.code == "retry_exhausted"
    assert events[0].error.retryable is False
    assert events[0].error.provider_details["retries"] == 1
