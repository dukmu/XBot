"""Typed process contracts owned by the server application."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from xcore.api.services import ServiceKey

from fastapi import APIRouter, Request
from starlette.responses import Response
from xcore.api.context import Context
from xcore.api.events import EventKey
from XBotv2.core.api.operations import EmptyRequest

ExceptionHandler = Callable[[Request, Exception], Awaitable[Response]]


@dataclass(frozen=True, slots=True)
class RouteContribution:
    """One route contribution owned by a protocol plugin fiber."""

    owner: str
    router: APIRouter
    exception_handlers: tuple[
        tuple[type[Exception], ExceptionHandler], ...
    ] = ()


async def contribute_router(
    ctx: Context,
    *,
    owner: str,
    router: APIRouter,
    exception_handlers: tuple[
        tuple[type[Exception], ExceptionHandler], ...
    ] = (),
) -> None:
    """Register an adapter router and bind cleanup to its XCore fiber."""
    result = await ctx.bail(
        REGISTER_ROUTE,
        RouteContribution(owner, router, exception_handlers),
    )
    if not callable(result):
        raise RuntimeError("HTTP route carrier is unavailable")
    ctx.dispose(result)


@dataclass(frozen=True, slots=True)
class ServerInfo:
    """Transport-owned process metadata exposed to the core router."""

    name: str
    started_at: float


@dataclass(frozen=True, slots=True)
class ServerStatus:
    """Capability-neutral health projection contributed by Sessions."""

    sessions: int
    threads: int
    workspace_root: str


QUERY_STATUS = EventKey[EmptyRequest]("server/status")
REGISTER_ROUTE = EventKey[RouteContribution]("http/route")

__all__ = [
    "ExceptionHandler",
    "QUERY_STATUS",
    "REGISTER_ROUTE",
    "RouteContribution",
    "ServerInfo",
    "ServerStatus",
    "contribute_router",
]


class HttpListener(Protocol):
    async def serve(self, *, host: str = "127.0.0.1", port: int = 4096, uds: str | None = None) -> None: ...
    async def stop(self) -> None: ...

HTTP_LISTENER: ServiceKey[HttpListener] = ServiceKey("server.listener")
