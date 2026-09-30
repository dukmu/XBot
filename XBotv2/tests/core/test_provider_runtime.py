"""Behavioral checks for provider retry and stream failure semantics."""

import asyncio
import json
from contextlib import asynccontextmanager

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


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["openai", "anthropic"])
@pytest.mark.parametrize("mode, last_code", [("idle", "ReadTimeout"), ("headers", "APITimeoutError")])
async def test_sdk_idle_timeout_uses_shared_retry_policy(adapter, mode, last_code):
    async with streaming_provider(adapter, mode, timeout=0.05) as (provider, calls):
        events = [event async for event in provider.astream(request())]

    assert len(calls) == 2
    assert len(events) == 1
    assert isinstance(events[0], ModelFailed)
    assert events[0].error.code == "retry_exhausted"
    assert events[0].error.category == "transport"
    assert events[0].error.provider_details["last_code"] == last_code


@pytest.mark.asyncio
async def test_sdk_timeout_retry_can_recover_and_complete():
    async with streaming_provider("openai", "recover", timeout=0.15) as (provider, calls):
        events = [event async for event in provider.astream(request())]

    assert len(calls) == 2
    assert isinstance(events[-1], ModelCompleted)
    assert not any(isinstance(event, ModelFailed) for event in events)


@pytest.mark.asyncio
async def test_live_reasoning_stream_can_outlast_transport_timeout():
    from XBotv2.core.stream import ReasoningDelta

    async with streaming_provider("openai", "live", timeout=0.15) as (provider, calls):
        events = [event async for event in provider.astream(request())]

    assert len(calls) == 1
    assert sum(isinstance(event, ReasoningDelta) for event in events) == 12
    assert isinstance(events[-1], ModelCompleted)


@pytest.mark.asyncio
async def test_sdk_timeout_after_output_does_not_replay_partial_response():
    async with streaming_provider("openai", "partial", timeout=0.05) as (provider, calls):
        events = [event async for event in provider.astream(request())]

    assert len(calls) == 1
    assert isinstance(events[0], TextDelta)
    assert isinstance(events[-1], ModelFailed)
    assert events[-1].error.category == "transport"


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["openai", "anthropic"])
async def test_null_timeout_disables_sdk_deadlines(adapter):
    async with streaming_provider(adapter, "idle", timeout=None) as (provider, _):
        assert provider.client.timeout is None


@asynccontextmanager
async def streaming_provider(adapter, mode, *, timeout):
    """Exercise real SDK HTTP/SSE reads, not a synthetic TimeoutError."""
    from XBotv2.llm.anthropic import AnthropicProvider
    from XBotv2.llm.openai import OpenAICompatibleProvider

    calls = []
    tasks = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.split(b":", 1)[1])
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            calls.append(json.loads(await reader.readexactly(length)))
            if mode == "headers":
                await reader.read()
                return
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
            await writer.drain()
            stream_mode = ("live" if len(calls) > 1 else "idle") if mode == "recover" else mode
            if stream_mode in ("live", "partial"):
                for _ in range(12 if stream_mode == "live" else 1):
                    delta = {"reasoning_content": "thinking"} if stream_mode == "live" else {"content": "partial"}
                    chunk = {"id": "test", "model": "test-model", "created": 0,
                             "object": "chat.completion.chunk",
                             "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                    writer.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    await writer.drain()
                    await asyncio.sleep(0.03)
            if stream_mode == "live":
                writer.write(b"data: [DONE]\n\n")
                await writer.drain()
            else:
                await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    provider_type = OpenAICompatibleProvider if adapter == "openai" else AnthropicProvider
    provider = provider_type(
        api_key="local-test", base_url=f"http://127.0.0.1:{port}",
        request_timeout_seconds=timeout, max_retries=1, retry_backoff_factor=0,
    )
    try:
        yield provider, calls
    finally:
        await provider.client.close()
        server.close()
        await server.wait_closed()
        pending = list(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
