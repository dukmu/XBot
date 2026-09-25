"""Usage aggregation over provider-neutral request observations."""

import pytest
from xcore import Context
from xcore.state import StateService

from XBotv2.core.domain import (
    AuxiliaryRequest,
    GenerationSettings,
    MeasurementUnavailable,
    ModelRoute,
    ProviderMeasured,
    RequestObservation,
    ResolvedModelSelection,
    StandardGenerationMode,
    TokenCounters,
    TurnRequest,
    TurnId,
    UsageDelta,
    UsageSnapshot,
)
from XBotv2.usage.plugin import UsageService


def _selection() -> ResolvedModelSelection:
    return ResolvedModelSelection(
        route=ModelRoute(provider="mock", model="test"),
        generation=GenerationSettings(
            mode=StandardGenerationMode(), max_output_tokens=128,
        ),
        context_window=4096,
    )


def _observation(purpose, context=0) -> RequestObservation:
    observed = (
        ProviderMeasured(tokens=context)
        if context is not None
        else MeasurementUnavailable(reason="provider omitted context usage")
    )
    return RequestObservation(
        selection=_selection(),
        purpose=purpose,
        estimated_input_tokens=0,
        observed_context=observed,
    )


def test_token_counters_adds_each_domain_counter():
    current = TokenCounters(
        input=10,
        output=4,
        cache_read=3,
        cache_create=2,
        prompt_cache_write=1,
    )
    delta = TokenCounters(
        input=5,
        output=6,
        cache_read=7,
        cache_create=8,
        prompt_cache_write=9,
    )

    assert current.add(delta) == TokenCounters(
        input=15,
        output=10,
        cache_read=10,
        cache_create=10,
        prompt_cache_write=10,
    )


@pytest.mark.asyncio
async def test_usage_records_typed_deltas_and_restores_one_snapshot(tmp_path):
    store = StateService(path=tmp_path / "state.json").namespace("usage")
    usage = UsageService(store, Context())
    await usage.initialize(())
    observation = _observation(TurnRequest(turn_id=TurnId("turn-1")), 24)

    snapshot = await usage.record(
        observation,
        UsageDelta(counters=TokenCounters(
            input=10,
            output=4,
            cache_read=2,
            cache_create=3,
            prompt_cache_write=1,
        )),
    )
    assert snapshot.total_counters == TokenCounters(
        input=10,
        output=4,
        cache_read=2,
        cache_create=3,
        prompt_cache_write=1,
    )
    assert snapshot.latest_turn_observation == observation
    assert snapshot.requests == (observation,)

    restored = UsageService(store, Context())
    await restored.initialize(())
    assert restored.snapshot() == snapshot


@pytest.mark.asyncio
async def test_auxiliary_request_accumulates_without_replacing_turn_context(tmp_path):
    usage = UsageService(
        StateService(path=tmp_path / "state.json").namespace("usage"), Context(),
    )
    await usage.initialize(())
    turn = _observation(TurnRequest(turn_id=TurnId("turn-1")), 100)
    auxiliary = _observation(
        AuxiliaryRequest(owner="caption", operation_id="title-1"), None,
    )

    await usage.record(turn, UsageDelta(counters=TokenCounters(input=100, output=10)))
    snapshot = await usage.record(
        auxiliary,
        UsageDelta(counters=TokenCounters(input=20, output=5)),
    )

    assert snapshot.total_counters == TokenCounters(input=120, output=15)
    assert snapshot.latest_turn_observation == turn
    assert snapshot.requests == (turn, auxiliary)
    assert snapshot.requests[-1].observed_context == MeasurementUnavailable(
        reason="provider omitted context usage",
    )


@pytest.mark.asyncio
async def test_usage_service_requires_initialization_before_recording(tmp_path):
    usage = UsageService(
        StateService(path=tmp_path / "state.json").namespace("usage"), Context(),
    )
    with pytest.raises(RuntimeError, match="initialized"):
        await usage.record(
            _observation(TurnRequest(turn_id=TurnId("turn-1"))), UsageDelta(
                counters=TokenCounters(),
            ),
        )


def test_usage_snapshot_starts_with_no_latest_turn_observation():
    assert UsageSnapshot().latest_turn_observation is None
