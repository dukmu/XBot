"""Core protocol routes contributed like every other HTTP capability."""

from __future__ import annotations

import time
from typing import Protocol

from fastapi import APIRouter
from xcore import Context

from XBotv2.core.errors import OperationError
from XBotv2.protocol.http_util import HttpServerError
from XBotv2.protocol.models import (
    HealthResponse,
    HelloRequest,
    HelloResponse,
)
from XBotv2.protocol.version import PROTOCOL_VERSION
from XBotv2.server.contracts import (
    QUERY_STATUS,
    ServerInfo,
    ServerStatus,
    contribute_router,
)


class EventDispatcher(Protocol):
    async def bail(self, event: str, *args: object) -> object: ...


def build_core_router(
    *,
    events: EventDispatcher,
    info: ServerInfo,
) -> APIRouter:
    router = APIRouter()

    @router.get("/health", operation_id="health")
    async def health() -> HealthResponse:
        status = await events.bail(QUERY_STATUS)
        if not isinstance(status, ServerStatus):
            raise OperationError(
                "capability_unavailable",
                "server status capability is unavailable",
            )
        return HealthResponse(
            status="ok",
            server_name=info.name,
            uptime_s=int(time.monotonic() - info.started_at),
            sessions=status.sessions,
            threads=status.threads,
            workspace_root=status.workspace_root,
        )

    @router.post("/hello", operation_id="hello")
    async def hello(payload: HelloRequest) -> HelloResponse:
        if payload.protocol_version != PROTOCOL_VERSION:
            raise HttpServerError(
                "unsupported_protocol",
                f"Protocol {payload.protocol_version!r} is not supported; "
                f"expected {PROTOCOL_VERSION!r}",
                status=426,
            )
        return HelloResponse(
            server_name=info.name,
            session_id=(payload.session_id or "").strip(),
            thread_id=payload.thread_id.strip() or "agent",
        )

    return router


class CoreHttpAdapter:
    name = "xbot.http.core"
    inject = ["server", "server_info"]

    async def apply(self, ctx: Context, config: object = None) -> None:
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_core_router(events=ctx, info=ctx.server_info),
        )


plugin = CoreHttpAdapter()


__all__ = ["build_core_router", "plugin"]
