"""Behavior tests for shared context accounting and TokenManager diagnostics."""

from types import SimpleNamespace

import pytest

from builtin_plugins.token_manager.plugin import TokenManagerPlugin
from xbotv2.api import (
    HookContext,
    HookStage,
    Message,
    PluginManifest,
    Tool,
    calibrated_context_tokens,
    context_token_limit,
    estimate_request_tokens,
)
from xbotv2.api.tokens import REQUEST_ESTIMATE_KEY


def make_plugin() -> TokenManagerPlugin:
    return TokenManagerPlugin(
        PluginManifest(name="token_manager", version="1"),
        store=None,
    )


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
    ctx = HookContext(
        stage=HookStage.BEFORE_MODEL_REQUEST,
        state={"messages": messages},
        config=SimpleNamespace(max_context_tokens=204_800),
        session=SimpleNamespace(turn_count=3),
        model_request={"messages": messages, "tools": []},
    )

    await plugin._on_before_model_request(ctx)
    ctx.model_response = SimpleNamespace(usage_metadata={
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


@pytest.mark.asyncio
async def test_plugin_unload_clears_only_ephemeral_observation():
    plugin = make_plugin()
    plugin._latest = {"context_tokens_estimate": 10}

    await plugin.on_unload()

    assert plugin.diagnostics() == {
        "status": "ready",
        "mode": "observe_only",
        "latest_request": {},
    }
