"""Typed process contracts owned by the server application."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response
from xcore import Disposer

from XBotv2.core.providers import BaseProvider

QUERY_STATUS = "server/status"
REGISTER_ROUTE = "http/route"
ExceptionHandler = Callable[[Request, Exception], Awaitable[Response]]


def current_model_override() -> BaseProvider | None:
    """FastAPI dependency used by tests and embedded server compositions."""
    return None


ModelOverride = Annotated[
    BaseProvider | None,
    Depends(current_model_override),
]


@dataclass(frozen=True, slots=True)
class RouteContribution:
    """One route contribution owned by a protocol plugin fiber."""

    owner: str
    router: APIRouter
    exception_handlers: tuple[
        tuple[type[Exception], ExceptionHandler], ...
    ] = ()


class RouteEventContext(Protocol):
    async def bail(self, event: str, *args: object) -> object: ...

    def dispose(self, callback: Disposer) -> Disposer: ...


async def contribute_router(
    ctx: RouteEventContext,
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
class ServerOptions:
    """Composition launch facts consumed by server-side capability plugins."""

    provider_name: str
    workspace_root: Path
    no_plugins: bool


@dataclass(frozen=True, slots=True)
class ServerStatus:
    """Capability-neutral health projection contributed by Sessions."""

    sessions: int
    threads: int
    workspace_root: str


__all__ = [
    "ExceptionHandler",
    "ModelOverride",
    "QUERY_STATUS",
    "REGISTER_ROUTE",
    "RouteContribution",
    "RouteEventContext",
    "ServerInfo",
    "ServerOptions",
    "ServerStatus",
    "contribute_router",
    "current_model_override",
]
