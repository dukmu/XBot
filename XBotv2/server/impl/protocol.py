"""Core protocol routes contributed like every other HTTP capability."""

from __future__ import annotations

import time
from xcore.api.events import EventQuery
from fastapi import APIRouter
from XBotv2.core.api.errors import OperationError
from XBotv2.core.api.operations import EmptyRequest
from XBotv2.protocol.api.http_util import HttpServerError
from XBotv2.protocol.api.models import HealthResponse, HelloRequest, HelloResponse
from XBotv2.protocol.api.version import PROTOCOL_VERSION
from XBotv2.server.api.contracts import QUERY_STATUS, ServerInfo, ServerStatus


def build_core_router(
    *,
    events: EventQuery,
    info: ServerInfo,
) -> APIRouter:
    router = APIRouter()

    @router.get("/health", operation_id="health")
    async def health() -> HealthResponse:
        status = await events.bail(QUERY_STATUS, EmptyRequest())
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


__all__ = ["build_core_router"]
