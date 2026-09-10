"""Behavior tests for shared context accounting and TokenManager diagnostics."""

import pytest

from XBotv2.token_manager.plugin import TokenManagerPlugin

from XBotv2.core import (
    Message,
    Tool,
    calibrated_context_tokens,
    context_token_limit,
    estimate_request_tokens,
)
from XBotv2.agentloop import EventContext, Events, LoopSettings, ModelRequest
from XBotv2.core.tokens import REQUEST_ESTIMATE_KEY
from XBotv2.llm.mock import MockLLM
from XBotv2.session import SessionInfo
from XBotv2.core import ModelResponse


def make_plugin() -> TokenManagerPlugin:
    from XBotv2.token_manager.plugin import TokenManagerPlugin

    return TokenManagerPlugin()


def test_context_limit_uses_ratio_and_provider_output_reservation():
    assert context_token_limit(
        1_048_576,
        trigger_ratio=0.8,
    ) == 838_860
    assert context_token_limit(
        200_000,
        trigger_ratio=0.8,
        output_reservation=64_000,
    ) == 136_000


def test_request_estimate_includes_messages_reasoning_and_tool_schema():
    def echo(value: str) -> str:
        """Echo one value."""
        return value

    plain = [Message(role="user", content="hello")]
    enriched = [
        *plain,
        Message(
            role="assistant",
            content="done",
            reasoning="consider constraints",
        ),
    ]

    assert estimate_request_tokens(
        enriched,
        [Tool.from_function(echo)],
    ) > estimate_request_tokens(plain)


def test_context_estimate_reuses_latest_provider_measurement():
    previous = Message(
        role="assistant",
        content="previous",
        usage_metadata={"context_tokens": 150_000},
        response_metadata={REQUEST_ESTIMATE_KEY: 100_000},
    )
    current = [Message(role="system", content="x" * 300_000)]
    raw = estimate_request_tokens(current)

    context, estimate, source = calibrated_context_tokens(
        current,
        [],
        [previous],
    )

    assert estimate == raw
    assert context == 150_000 + raw - 100_000
    assert source == "provider_calibrated"


@pytest.mark.asyncio
async def test_plugin_observes_runtime_window_and_provider_usage():
    plugin = make_plugin()
    messages = [Message(role="user", content="hello")]
    ctx = EventContext(
        messages=messages,
        settings=LoopSettings(provider="test", context_window=204_800),
        session=SessionInfo("s", "t", provider="test", turn_count=3),
        model_request=ModelRequest(messages, [], MockLLM(responses=[])),
    )

    await plugin._on_model_request_ready(ctx)
    ctx.model_response = ModelResponse(usage_metadata={
        "input_tokens": 100,
        "output_tokens": 20,
        "context_tokens": 180,
        "cache_read_input_tokens": 80,
        "total_tokens": 200,
    })
    await plugin._on_after_model_response(ctx)

    latest = plugin.diagnostics()["latest_request"]
    assert latest["context_window"] == 204_800
    assert latest["estimate_source"] == "estimated"
    assert latest["provider_usage"]["context_tokens"] == 180
    assert latest["provider_usage"]["cache_read_input_tokens"] == 80
