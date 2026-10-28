from XBotv2.tests.runtime import engine_of
"""Token manager observes typed requests; it does not own context policy."""

from types import SimpleNamespace
import pytest
from xcore.api.context import Context
from XBotv2.agentloop.api.contracts import HumanInput, InboxItem
from XBotv2.core.api.domain import (
    InboxTarget,
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
from XBotv2.agentloop.api.events import (
    Events, ModelRequestReady, ModelResponseObserved, ReplaceContext, ReplaceRequest, RetryRequest,
)
from XBotv2.tests.runtime import thread_scope
from XBotv2.core.api.parts import TextPart
from XBotv2.core.api.provider import ModelRequest, ProviderUser, ToolSchema
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.tokens import estimate_model_request_tokens
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.token_manager.impl.plugin import TokenManagerPlugin


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
    application = await thread_scope(
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
        [event async for event in engine_of(application).run_turn(InboxItem(
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
        await application.root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["normal", "replace", "replace_context", "retry", "retry_replace", "finalize"])
async def test_engine_shares_candidate_estimate_with_observers(
    temp_data_dir, temp_workspace, monkeypatch, scenario,
):
    from XBotv2.agentloop.impl import engine as engine_module
    from XBotv2.core.api.stream import ModelCompleted

    class Provider(MockLLM):
        async def _astream_once(self, request):
            self._state.call_count += 1
            self._state.request_history.append(request)
            if scenario.startswith("retry") and self.call_count == 1:
                raise RuntimeError("retry this request")
            if scenario == "finalize" and self.call_count == 1:
                response = {"tool_calls": [{"id": "one", "name": "missing", "args": {}}]}
            else:
                response = {"content": "done"}
            yield ModelCompleted(response=self.to_response(response))

    provider = Provider([])
    application = await thread_scope(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="estimate-sharing", workspace_root=temp_workspace,
        plugin_dirs=[], llm_override=provider,
        extra_plugins=[{"id": "caption", "config": {"auto": False}}],
    )
    estimates, ready, observed = [], [], []
    original_estimator = engine_module.estimate_request_tokens

    def estimate(messages, tools):
        value = original_estimator(messages, tools)
        estimates.append((messages, tools, value))
        return value

    async def before(event):
        if scenario in ("replace", "finalize"):
            extra = ProviderUser(parts=(TextPart(text="hook replacement"),))
            return ReplaceRequest(event.request.model_copy(update={"messages": (*event.request.messages, extra)}))

    async def after_context(event):
        if scenario == "replace_context":
            return ReplaceContext((*event.context, ProviderUser(parts=(TextPart(text="context replacement"),))))

    async def retry(event):
        request = event.request
        if scenario == "retry_replace":
            extra = ProviderUser(parts=(TextPart(text="retry replacement"),))
            request = request.model_copy(update={"messages": (*request.messages, extra)})
        return RetryRequest(request)

    async def on_ready(event):
        ready.append((event.request, event.estimated_input_tokens))

    async def on_response(event):
        observed.append(event.exchange.observation.estimated_input_tokens)

    monkeypatch.setattr(engine_module, "estimate_request_tokens", estimate)
    application.on(Events.BEFORE_MODEL_REQUEST, before)
    application.on(Events.AFTER_CONTEXT_BUILD, after_context)
    application.on(Events.MODEL_REQUEST_ERROR, retry)
    application.on(Events.MODEL_REQUEST_READY, on_ready)
    application.on(Events.MODEL_RESPONSE_OBSERVED, on_response)
    engine = engine_of(application)
    if scenario == "finalize":
        engine.configure(max_iterations=1)
    try:
        [event async for event in engine.run_turn(InboxItem(
            target=InboxTarget.NEXT_TURN, input=HumanInput(content="test estimates"),
        ))]
        requests = provider.request_history
        expected_candidates = 2 if scenario in ("retry_replace", "finalize") else 1
        assert len(estimates) == len(ready) == expected_candidates
        for request, value in ready:
            assert (request.messages, request.tools, value) in estimates
            assert value == estimate_model_request_tokens(request)
        assert ready[-1][0] == requests[-1]
        assert observed[-1] == ready[-1][1]
        latest = application.token_manager.diagnostics()["latest_request"]
        assert latest["estimated_context"]["tokens"] == observed[-1]
        assert latest["tool_count"] == len(requests[-1].tools)
        if scenario == "replace_context":
            assert requests[0].messages[-1] == ProviderUser(parts=(TextPart(text="context replacement"),))
        if scenario == "finalize":
            assert requests[0].tools
            assert not requests[-1].tools
            assert requests[-1].messages[0].role == "system"
            assert requests[-1].messages[1].role == "system"
            assert "Iteration limit" in requests[-1].messages[1].parts[0].text
    finally:
        await application.root.close()


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
        estimated_input_tokens=estimate_model_request_tokens(request),
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
        estimated_input_tokens=estimate_model_request_tokens(request),
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
        estimated_input_tokens=estimate_model_request_tokens(request),
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
        estimated_input_tokens=estimate_model_request_tokens(request),
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
        estimated_input_tokens=5,
    ))
    assert plugin.diagnostics()["latest_request"]

    await context.root.close()

    assert plugin.diagnostics()["latest_request"] == {}
