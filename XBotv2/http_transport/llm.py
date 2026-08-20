"""HTTP adapter for LLM catalog and session binding operations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from XBotv2.core.operations import EmptyRequest, dispatch_operation
from XBotv2.server.contracts import contribute_router
from XBotv2.llm.contracts import (
    LIST_PROVIDERS,
    SELECT_EFFORT,
    SELECT_PROVIDER,
    SelectEffort,
    SelectProvider,
)
from XBotv2.protocol.http_util import HttpServerError
from XBotv2.protocol.models import (
    EffortSelectionRequest,
    EffortSelectionResponse,
    ModelInfo,
    ProviderInfo,
    ProviderListResponse,
    ProviderSelectionRequest,
    ProviderSelectionResponse,
)
from XBotv2.session.contracts import SessionRef, dispatch_session_operation


def build_router(*, events: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/providers", operation_id="list_providers")
    async def list_providers() -> ProviderListResponse:
        catalog = await dispatch_operation(events, LIST_PROVIDERS, EmptyRequest())
        return ProviderListResponse(
            default=catalog.default,
            providers=[
                ProviderInfo(
                    name=provider.name,
                    provider=provider.protocol,
                    default_model=provider.default_model,
                    models=[
                        ModelInfo(
                            model=model.model,
                            max_context_tokens=model.max_context_tokens,
                            max_output_tokens=model.max_output_tokens,
                            reasoning_effort=model.reasoning_effort,
                            effort=list(model.effort),
                            thinking=model.thinking,
                            input_modalities=list(model.input_modalities),
                        )
                        for model in provider.models
                    ],
                )
                for provider in catalog.providers
            ],
        )

    @router.put(
        "/sessions/{session_id}/threads/{thread_id}/provider",
        operation_id="select_provider",
    )
    async def select_provider(
        session_id: str,
        thread_id: str,
        payload: ProviderSelectionRequest,
    ) -> ProviderSelectionResponse:
        try:
            selected = await dispatch_session_operation(
                events,
                SessionRef(session_id, thread_id),
                SELECT_PROVIDER,
                SelectProvider(payload.name, payload.model),
            )
        except ValueError as exc:
            code = "model_not_found" if "Unknown model" in str(exc) else "provider_not_found"
            raise HttpServerError(code, str(exc), status=404) from exc
        return ProviderSelectionResponse(
            session_id=session_id,
            thread_id=thread_id,
            provider=selected.provider,
            model=selected.model,
            model_mode=selected.model_mode,
        )

    @router.put(
        "/sessions/{session_id}/threads/{thread_id}/effort",
        operation_id="select_effort",
    )
    async def select_effort(
        session_id: str,
        thread_id: str,
        payload: EffortSelectionRequest,
    ) -> EffortSelectionResponse:
        try:
            selected = await dispatch_session_operation(
                events,
                SessionRef(session_id, thread_id),
                SELECT_EFFORT,
                SelectEffort(payload.effort),
            )
        except ValueError as exc:
            raise HttpServerError("unsupported_effort", str(exc), status=400) from exc
        return EffortSelectionResponse(
            session_id=session_id,
            thread_id=thread_id,
            provider=selected.provider,
            model=selected.model,
            reasoning_effort=selected.reasoning_effort,
            model_mode=selected.model_mode,
            available=list(selected.available),
        )

    return router


class LlmHttpAdapter:
    name = "xbot.http.llm"
    inject = ["server", "llm"]

    async def apply(self, ctx: Any, config: Any = None) -> None:
        await contribute_router(ctx, owner=self.name, router=build_router(events=ctx))


plugin = LlmHttpAdapter()
