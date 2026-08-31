"""LLM C/S wire models and route contribution."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import Field

from XBotv2.core.operations import EmptyRequest, dispatch_operation
from XBotv2.llm.contracts import (
    LIST_PROVIDERS,
    SELECT_EFFORT,
    SELECT_PROVIDER,
    SelectEffort,
    SelectProvider,
)
from XBotv2.protocol.http_util import HttpServerError
from XBotv2.protocol import WireModel
from XBotv2.session import SessionsPort


class ModelInfo(WireModel):
    model: str = Field(min_length=1)
    max_context_tokens: int = Field(ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    reasoning_effort: str = ""
    effort: list[str] = Field(default_factory=list)
    thinking: str = ""
    input_modalities: list[Literal["text", "image"]] = Field(
        default_factory=lambda: ["text"]
    )


class ProviderInfo(WireModel):
    name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    default_model: str = Field(min_length=1)
    models: list[ModelInfo] = Field(default_factory=list)


class ProviderListResponse(WireModel):
    default: str
    providers: list[ProviderInfo] = Field(default_factory=list)


class ProviderSelectionRequest(WireModel):
    name: str = Field(min_length=1)
    model: str | None = Field(default=None, min_length=1)


class ProviderSelectionResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_mode: str = ""


class EffortSelectionRequest(WireModel):
    effort: str = Field(min_length=1)


class EffortSelectionResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: str = ""
    model_mode: str = ""
    available: list[str] = Field(default_factory=list)


def build_router(*, events: Any, sessions: SessionsPort) -> APIRouter:
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
            selected = await sessions.dispatch(
                session_id,
                thread_id,
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
            selected = await sessions.dispatch(
                session_id,
                thread_id,
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


__all__ = [
    "EffortSelectionRequest",
    "EffortSelectionResponse",
    "ModelInfo",
    "ProviderInfo",
    "ProviderListResponse",
    "ProviderSelectionRequest",
    "ProviderSelectionResponse",
    "build_router",
]
