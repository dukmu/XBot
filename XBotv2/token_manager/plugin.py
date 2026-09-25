"""Observe typed request estimates and provider context measurements."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import JsonValue
from xcore import Context

from XBotv2.agentloop import Events
from XBotv2.agentloop.events import ModelRequestReady, ModelResponseObserved
from XBotv2.core.domain import (
    MeasurementUnavailable,
    ObservedContext,
    ProviderMeasured,
    ResolvedModelSelection,
    TokenCounters,
)
from XBotv2.core.tokens import TokenBudget, estimate_model_request_tokens

from .contracts import EstimatedContext


@dataclass(frozen=True, slots=True)
class _RequestReady:
    turn: int
    message_count: int
    tool_count: int
    selection: ResolvedModelSelection
    estimate: EstimatedContext


@dataclass(frozen=True, slots=True)
class _ResponseObserved:
    request: _RequestReady
    usage: TokenCounters
    observed_context: ObservedContext


_LatestRequest = _RequestReady | _ResponseObserved


class TokenManagerPlugin:
    inject = ["session"]
    name = "token_manager"

    def __init__(self) -> None:
        self._latest: _LatestRequest | None = None

    def apply(
        self,
        ctx: Context,
        _config: dict[str, JsonValue] | None,
    ) -> None:
        ctx.set("token_manager", self)
        ctx.dispose(self._clear)
        ctx.on(Events.MODEL_REQUEST_READY, self._on_model_request_ready)
        ctx.on(Events.MODEL_RESPONSE_OBSERVED, self._on_after_model_response)

    def _clear(self) -> None:
        self._latest = None

    async def _on_model_request_ready(self, event: ModelRequestReady) -> None:
        request = event.request
        estimate = EstimatedContext(
            tokens=estimate_model_request_tokens(request),
            method="estimate_model_request_tokens",
        )
        self._latest = _RequestReady(
            turn=event.session.turn_count,
            message_count=len(request.messages),
            tool_count=len(request.tools),
            selection=request.selection,
            estimate=estimate,
        )

    async def _on_after_model_response(self, event: ModelResponseObserved) -> None:
        request = self._latest
        if not isinstance(request, _RequestReady):
            raise RuntimeError(
                "Token manager received a model response without a pending request"
            )
        exchange = event.exchange
        if exchange.observation.selection != request.selection:
            raise RuntimeError(
                "Token manager response selection does not match the pending request"
            )
        self._latest = _ResponseObserved(
            request=request,
            usage=exchange.usage.counters,
            observed_context=exchange.observation.observed_context,
        )

    def diagnostics(self) -> dict[str, JsonValue]:
        request = self._latest
        latest: dict[str, JsonValue] = {}
        if isinstance(request, _RequestReady):
            latest = _request_diagnostics(request)
        elif isinstance(request, _ResponseObserved):
            latest = _request_diagnostics(request.request)
            latest["response"] = _response_diagnostics(request)
        return {
            "status": "ready",
            "mode": "observe_only",
            "latest_request": latest,
        }


def _request_diagnostics(request: _RequestReady) -> dict[str, JsonValue]:
    budget = _budget(request.selection, request.estimate.tokens)
    return {
        "turn": request.turn,
        "message_count": request.message_count,
        "tool_count": request.tool_count,
        "estimated_context": request.estimate.model_dump(mode="json"),
        "estimated_budget": _budget_diagnostics(budget),
    }


def _response_diagnostics(response: _ResponseObserved) -> dict[str, JsonValue]:
    observed = response.observed_context
    result: dict[str, JsonValue] = {
        "provider_usage": response.usage.model_dump(mode="json"),
        "observed_context": observed.model_dump(mode="json"),
    }
    if isinstance(observed, ProviderMeasured):
        result["observed_budget"] = _budget_diagnostics(
            _budget(response.request.selection, observed.tokens)
        )
    elif not isinstance(observed, MeasurementUnavailable):
        raise TypeError(f"Unsupported observed context: {type(observed).__name__}")
    return result


def _budget(selection: ResolvedModelSelection, used: int) -> TokenBudget:
    return TokenBudget(
        context_window=selection.context_window,
        output_reservation=selection.generation.max_output_tokens,
        used=used,
    )


def _budget_diagnostics(budget: TokenBudget) -> dict[str, JsonValue]:
    return {
        **budget.model_dump(mode="json"),
        "remaining": budget.remaining,
        "over_budget": budget.over_budget,
        "excess": budget.excess,
    }


plugin = TokenManagerPlugin()
