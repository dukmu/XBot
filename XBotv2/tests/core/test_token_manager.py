"""Token manager observes typed requests; it does not own context policy."""

from types import SimpleNamespace

import pytest
from xcore import Context

from XBotv2.agentloop import HumanInput, InboxItem, InboxTarget
from XBotv2.agentloop.events import ModelRequestReady, ModelResponseObserved
from XBotv2.application.app import start_application
from XBotv2.core.domain import (
    CompletedStop,
    GenerationSettings,
    MeasurementUnavailable,
    ModelExchange,
    ModelRoute,
    ModelTiming,
    ProviderExtensions,
    ProviderMeasured,
    RequestObservation,
    ResolvedModelSelection,
    StandardGenerationMode,
    TokenCounters,
    TurnId,
    TurnRequest,
    UsageDelta,
)
from XBotv2.core.parts import TextPart
from XBotv2.core.provider import ModelRequest, ProviderUser, ToolSchema
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.tokens import estimate_model_request_tokens
from XBotv2.llm.mock import MockLLM
from XBotv2.token_manager.plugin import TokenManagerPlugin


def _selection(*, context_window=4096, max_output_tokens=128):
    return ResolvedModelSelection(
        route=ModelRoute(provider="mock", model="test"),
        generation=GenerationSettings(
            mode=StandardGenerationMode(), max_output_tokens=max_output_tokens,
        ),
        context_window=context_window,
    )


@pytest.mark.asyncio
async def test_application_exposes_diagnostics_from_the_actual_final_exchange(
    temp_data_dir,
    temp_workspace,
):
    provider = MockLLM(responses=[{
        "content": "done",
        "usage_metadata": {"input_tokens": 17, "output_tokens": 4},
    }])
    application = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="token-manager-diagnostics",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
        extra_plugins=[{
            "id": "caption",
            "config": {"auto": False, "allow_access": False},
        }],
    )
    try:
        [event async for event in application.engine.run_turn(InboxItem(
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content="inspect the final request"),
        ))]

        request = provider.request_history[-1]
        diagnostics = application.token_manager.diagnostics()
        latest = diagnostics["latest_request"]
        assert diagnostics["mode"] == "observe_only"
        assert latest["estimated_context"] == {
            "tokens": estimate_model_request_tokens(request),
            "method": "estimate_model_request_tokens",
        }
        assert latest["message_count"] == len(request.messages)
        assert latest["tool_count"] == len(request.tools)
        assert latest["response"]["provider_usage"] == {
            "input": 17,
            "output": 4,
            "cache_read": 0,
            "cache_create": 0,
            "prompt_cache_write": 0,
        }
        assert latest["response"]["observed_context"] == {
            "kind": "measurement_unavailable",
            "reason": "mock provider",
        }
    finally:
        await application.destroy()


@pytest.mark.asyncio
async def test_request_diagnostics_are_derived_from_provider_request():
    plugin = TokenManagerPlugin()
    request = ModelRequest(
        messages=(ProviderUser(parts=(TextPart(text="hello world"),)),),
        tools=(ToolSchema(
            name="large_schema",
            description="A deliberately detailed tool schema. " * 32,
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        ),),
        selection=_selection(),
    )
    await plugin._on_model_request_ready(ModelRequestReady(
        request=request,
        session=SimpleNamespace(turn_count=3),
    ))

    latest = plugin.diagnostics()["latest_request"]
    assert latest["turn"] == 3
    assert latest["message_count"] == 1
    assert latest["tool_count"] == 1
    estimate = estimate_model_request_tokens(request)
    assert latest["estimated_context"] == {
        "tokens": estimate,
        "method": "estimate_model_request_tokens",
    }
    assert latest["estimated_budget"] == {
        "context_window": 4096,
        "output_reservation": 128,
        "used": estimate,
        "remaining": 4096 - 128 - estimate,
        "over_budget": estimate + 128 > 4096,
        "excess": max(0, estimate + 128 - 4096),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("observed_tokens", [0, 7])
async def test_response_diagnostics_keep_typed_usage_and_observed_context(
    observed_tokens,
):
    plugin = TokenManagerPlugin()
    request = ModelRequest(
        messages=(ProviderUser(parts=(TextPart(text="hello"),)),),
        tools=(),
        selection=_selection(),
    )
    await plugin._on_model_request_ready(ModelRequestReady(
        request=request,
        session=SimpleNamespace(turn_count=1),
    ))
    observation = RequestObservation(
        selection=_selection(),
        purpose=TurnRequest(turn_id=TurnId("turn-1")),
        estimated_input_tokens=5,
        observed_context=ProviderMeasured(tokens=observed_tokens),
    )
    exchange = ModelExchange(
        observation=observation,
        usage=UsageDelta(counters=TokenCounters(input=7, output=2)),
        timing=ModelTiming(total_ms=10),
        stop=CompletedStop(),
        provider_extensions=ProviderExtensions(provider="mock"),
    )

    await plugin._on_after_model_response(ModelResponseObserved(exchange))
    latest = plugin.diagnostics()["latest_request"]
    assert latest["response"]["provider_usage"] == {
        "input": 7,
        "output": 2,
        "cache_read": 0,
        "cache_create": 0,
        "prompt_cache_write": 0,
    }
    assert latest["response"]["observed_context"] == {
        "kind": "provider_measured", "tokens": observed_tokens,
    }
    assert latest["response"]["observed_budget"] == {
        "context_window": 4096,
        "output_reservation": 128,
        "used": observed_tokens,
        "remaining": 4096 - 128 - observed_tokens,
        "over_budget": observed_tokens + 128 > 4096,
        "excess": max(0, observed_tokens + 128 - 4096),
    }


@pytest.mark.asyncio
async def test_unavailable_measurement_does_not_become_an_estimated_observation():
    plugin = TokenManagerPlugin()
    request = ModelRequest(
        messages=(ProviderUser(parts=(TextPart(text="request text"),)),),
        tools=(),
        selection=_selection(),
    )
    await plugin._on_model_request_ready(ModelRequestReady(
        request=request,
        session=SimpleNamespace(turn_count=4),
    ))
    exchange = ModelExchange(
        observation=RequestObservation(
            selection=_selection(),
            purpose=TurnRequest(turn_id=TurnId("turn-4")),
            estimated_input_tokens=estimate_model_request_tokens(request),
            observed_context=MeasurementUnavailable(reason="provider did not report context"),
        ),
        usage=UsageDelta(counters=TokenCounters(input=12, output=3)),
        timing=ModelTiming(total_ms=10),
        stop=CompletedStop(),
        provider_extensions=ProviderExtensions(provider="mock"),
    )

    await plugin._on_after_model_response(ModelResponseObserved(exchange))

    latest = plugin.diagnostics()["latest_request"]
    assert latest["response"]["observed_context"] == {
        "kind": "measurement_unavailable",
        "reason": "provider did not report context",
    }
    assert "observed_budget" not in latest["response"]
    assert latest["estimated_context"]["tokens"] == estimate_model_request_tokens(request)


@pytest.mark.asyncio
async def test_estimated_budget_preserves_negative_remaining_for_compaction():
    plugin = TokenManagerPlugin()
    request = ModelRequest(
        messages=(ProviderUser(parts=(TextPart(text="x" * 800),)),),
        tools=(),
        selection=_selection(context_window=64, max_output_tokens=32),
    )
    await plugin._on_model_request_ready(ModelRequestReady(
        request=request,
        session=SimpleNamespace(turn_count=2),
    ))

    budget = plugin.diagnostics()["latest_request"]["estimated_budget"]

    assert budget["remaining"] < 0
    assert budget["over_budget"] is True
    assert budget["excess"] == -budget["remaining"]


@pytest.mark.asyncio
async def test_plugin_unload_clears_latest_request_state():
    context = Context()
    plugin = TokenManagerPlugin()
    plugin.apply(context, {})
    await plugin._on_model_request_ready(ModelRequestReady(
        request=ModelRequest(
            messages=(ProviderUser(parts=(TextPart(text="hello"),)),),
            tools=(),
            selection=_selection(),
        ),
        session=SimpleNamespace(turn_count=5),
    ))
    assert plugin.diagnostics()["latest_request"]

    await context.destroy()

    assert plugin.diagnostics()["latest_request"] == {}
