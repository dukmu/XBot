"""Stress infrastructure must traverse the real production provider adapter."""

from __future__ import annotations

import asyncio
import json
import httpx
import pytest
from XBotv2.core.api.domain import (
    GenerationSettings,
    ModelRoute,
    ResolvedModelSelection,
    StandardGenerationMode,
)
from XBotv2.core.api.parts import TextPart
from XBotv2.core.api.provider import ModelRequest, ProviderUser
from XBotv2.core.api.stream import ModelCompleted, ModelFailed, ReasoningDelta
from XBotv2.llm.openai import OpenAICompatibleProvider
from scripts.stress.config import parse_args
from scripts.stress.provider import NetworkMockLLM
from scripts.stress.scenarios import _submit_and_collect, run_stream_failure
from scripts.stress.server import LiveMockServerHarness, MockServerHarness


@pytest.mark.asyncio
@pytest.mark.parametrize("mock_tools", [False, True])
async def test_harness_streams_production_provider_http_contract(mock_tools):
    async with LiveMockServerHarness(5, mock_tools=mock_tools) as harness:
        harness.provider.chunk_delay_ms = 10
        harness.provider.jitter_ms = 5
        client = harness.client
        assert client is not None
        opened = (await client.open_session(session_id="wire-contract", thread_id="main")).data
        assert opened.metadata.workspace_root == str(harness.workspace)
        events = await _submit_and_collect(
            client, "wire-contract", opened.key.thread_id, "exercise the provider socket",
            after=opened.event_cursor, timeout=5, request_id="wire-turn",
        )
        assert events[-1].kind == "turn_ended"
        assert events[-1].payload["outcome"]["kind"] == "finished"
        assert not [event for event in events if event.kind == "error"]
        text = [event.payload["text"] for event in events if event.kind == "assistant_text_delta"]
        reasoning = [event.payload["text"] for event in events if event.kind == "assistant_reasoning_delta"]
        assert text == ["stress ", "response"]
        assert reasoning == ["stress ", "reasoning"] * (2 if mock_tools else 1)
        usage = [event.payload for event in events if event.kind == "usage"]
        assert usage[-1]["counters"]["input"] == 12
        assert usage[-1]["counters"]["output"] == 4

        request = harness.provider.requests[0]
        assert request["model"] == "stress-model"
        assert request["stream"] is True
        assert request["stream_options"] == {"include_usage": True}
        assert request["max_tokens"] == 4096
        shell = next(tool for tool in request["tools"] if tool["function"]["name"] == "shell")
        assert shell["type"] == "function"
        assert shell["function"]["parameters"]["type"] == "object"
        assert "command" in shell["function"]["parameters"]["properties"]
        if mock_tools:
            deltas = [event.payload for event in events if event.kind == "tool_call_delta"]
            assert len(deltas) == 3
            assert json.loads("".join(delta["arguments_delta"] for delta in deltas)) == {
                "command": "printf stress-tool",
            }
            result = next(event.payload for event in events if event.kind == "tool_completed")
            assert result["outcome"]["kind"] == "succeeded"
            assert result["outcome"]["output"]["parts"][0]["text"].strip() == "stress-tool"
            assert harness.provider.request_count == 2
            assert harness.provider.requests[1]["messages"][-1]["role"] == "tool"
        else:
            assert harness.provider.request_count == 1
        history = await client.list_messages("wire-contract", "main", limit=20)
        answer = next(item for item in reversed(history.items) if item.kind == "assistant")
        assert answer.content == "stress response"
        assert answer.reasoning == "stress reasoning"
        await client.delete_session("wire-contract")
    assert harness.provider.listener.task.done()
    assert harness.provider.listener.socket.fileno() == -1
    assert not harness.data_dir.exists()
    async with httpx.AsyncClient(trust_env=False) as probe:
        with pytest.raises(httpx.ConnectError):
            await probe.get(harness.provider.base_url)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["provider", "http_truncate"])
async def test_harness_failure_is_observable_and_followup_recovers(failure_mode):
    config = parse_args(["--scenario", "stream_failure", "--failure-mode", failure_mode, "--timeout", "5"])
    async with LiveMockServerHarness(5, failure_mode=failure_mode) as harness:
        report = await run_stream_failure(harness.client, config)
        assert not report.failed, report.as_dict()
        assert report.invariants["failure_was_observable"]
        assert report.invariants["followup_input_usable"]
        assert report.invariants["recovery_not_stuck_running"]
        assert harness.provider.request_count >= 2


@pytest.mark.asyncio
async def test_asgi_xbot_transport_still_uses_provider_socket():
    async with MockServerHarness(5) as harness:
        client = harness.client
        assert (await client.health()).status == "ok"
        await client.open_session(session_id="asgi-provider", thread_id="main")
        await client.send_message("asgi-provider", "main", "ASGI submission, TCP provider")
        async with asyncio.timeout(5):
            while True:
                thread = await client.get_thread("asgi-provider", "main")
                if thread.turn_status == "idle" and thread.message_count >= 2:
                    break
                await asyncio.sleep(0.01)
        history = await client.list_messages("asgi-provider", "main", limit=20)
        assert history.items[-1].content == "stress response"
        assert harness.provider.requests[0]["model"] == "stress-model"
        await client.delete_session("asgi-provider")


@pytest.mark.asyncio
async def test_harness_releases_provider_and_temp_root_on_application_start_failure(monkeypatch):
    async def fail_start(**kwargs):
        raise RuntimeError("application startup failed")

    monkeypatch.setattr("scripts.stress.server.start_server_application", fail_start)
    harness = MockServerHarness(5)
    with pytest.raises(RuntimeError, match="application startup failed"):
        await harness.__aenter__()
    assert harness.provider.listener.task.done()
    assert harness.provider.listener.socket.fileno() == -1
    assert not harness.data_dir.parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["disconnect", "http_error", "invalid_json", "invalid_utf8", "timeout"])
async def test_network_faults_cross_production_adapter_and_recover(fault):
    request = ModelRequest(
        messages=(ProviderUser(parts=(TextPart(text="network fault"),)),), tools=(),
        selection=ResolvedModelSelection(
            route=ModelRoute(provider="stress", model="stress-model"),
            generation=GenerationSettings(mode=StandardGenerationMode(), max_output_tokens=128),
            context_window=4096,
        ),
    )
    async with NetworkMockLLM(fault=fault, chunk_delay_ms=1) as network:
        provider = OpenAICompatibleProvider(
            api_key="stress", base_url=network.base_url, max_retries=0,
            request_timeout_seconds=0.05 if fault == "timeout" else 5,
        )
        try:
            first = [event async for event in provider.astream(request)]
            assert isinstance(first[-1], ModelFailed), first
            assert not any(isinstance(event, ModelCompleted) for event in first)
            if fault in {"disconnect", "invalid_json", "invalid_utf8"}:
                assert isinstance(first[0], ReasoningDelta)
            recovered = [event async for event in provider.astream(request)]
            assert isinstance(recovered[-1], ModelCompleted), recovered
            assert network.request_count == 2
        finally:
            await provider.client.close()
