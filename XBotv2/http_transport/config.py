"""HTTP adapter for session runtime configuration operations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from XBotv2.loader.contracts import RELOAD_PLUGINS
from XBotv2.core.operations import EmptyRequest
from XBotv2.server.contracts import contribute_router
from XBotv2.protocol.models import ConfigReloadResponse
from XBotv2.session.contracts import SessionRef, dispatch_session_operation


def build_router(*, events: Any) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/config/reload",
        operation_id="reload_config",
    )
    async def reload_config(
        session_id: str,
        thread_id: str,
    ) -> ConfigReloadResponse:
        reloaded = await dispatch_session_operation(
            events,
            SessionRef(session_id, thread_id),
            RELOAD_PLUGINS,
            EmptyRequest(),
        )
        return ConfigReloadResponse(
            session_id=session_id,
            thread_id=thread_id,
            reloaded=list(reloaded.reloaded),
            provider=reloaded.provider,
            model=reloaded.model,
            model_mode=reloaded.model_mode,
            context_window=reloaded.context_window,
            errors=list(reloaded.errors),
        )

    return router


class ConfigHttpAdapter:
    name = "xbot.http.config"
    inject = ["server"]

    async def apply(self, ctx: Any, config: Any = None) -> None:
        await contribute_router(ctx, owner=self.name, router=build_router(events=ctx))


plugin = ConfigHttpAdapter()
