"""Server root component exposing the HTTP/SSE protocol as ``ctx.server``.

The plugin is the dumb carrier: it builds the empty FastAPI application and
exposes it as ``ctx.server``. Capability protocol plugins contribute routes
through the typed ``http/route`` XCore event; business state and session
lifecycle do not belong to the carrier.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import partial
import time
from pydantic import JsonValue

from fastapi import APIRouter, FastAPI
from starlette.routing import BaseRoute
from xcore import Context, Disposer

from XBotv2.server.contracts import (
    REGISTER_ROUTE,
    RouteContribution,
)
from XBotv2.server.contracts import ServerInfo
from XBotv2.server.protocol import build_core_router


async def mount_core_http(ctx: Context) -> None:
    await ctx.emit(
        REGISTER_ROUTE,
        RouteContribution(
            owner="xbot.server.http",
            router=build_core_router(events=ctx, info=ctx.server_info),
        ),
    )


class WebServer:
    """Route-registration view over the FastAPI carrier app.

    Mirrors the DSH ``webServer`` service: registration is an effect (the
    disposer removes the contribution), and a duplicate path is a
    composition-level misconfiguration.
    """

    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def register(self, router: APIRouter) -> Disposer:
        """Mount an ``APIRouter`` and return the disposer that unmounts it.

        A route that duplicates an existing path+method is a composition-level
        misconfiguration and raises before any route is added. The disposer
        removes exactly the routes this router added.
        """
        existing = route_keys(self.app)
        incoming = route_keys(router)
        conflicts = sorted(incoming & existing)
        if conflicts:
            raise RuntimeError(
                "web_server route collision: "
                + ", ".join(f"{method} {path}" for method, path in conflicts)
            )
        before = list(self.app.routes)
        self.app.include_router(router)
        added = [route for route in self.app.routes if route not in before]
        return _remove_routes(self.app, added)

    def register_contribution(self, contribution: RouteContribution) -> Disposer:
        """Mount routes and exception handlers as one disposable effect."""
        if not isinstance(contribution, RouteContribution):
            raise TypeError("http/route requires RouteContribution")
        conflicts = [
            error_type.__name__
            for error_type, _handler in contribution.exception_handlers
            if error_type in self.app.exception_handlers
        ]
        if conflicts:
            raise RuntimeError(
                "web_server exception handler collision: " + ", ".join(conflicts)
            )
        dispose_routes = self.register(contribution.router)
        for error_type, handler in contribution.exception_handlers:
            self.app.add_exception_handler(error_type, handler)

        return partial(
            self._remove_contribution, contribution, dispose_routes
        )

    def _remove_contribution(
        self, contribution: RouteContribution, dispose_routes: Disposer
    ) -> bool:
        dispose_routes()
        for error_type, handler in contribution.exception_handlers:
            if self.app.exception_handlers.get(error_type) is handler:
                self.app.exception_handlers.pop(error_type, None)
        return True


def route_keys(owner: FastAPI | APIRouter) -> set[tuple[str, str]]:
    """Return the ``(method, path)`` pairs an application or router dispatches.

    ``include_router`` used to copy a child router's routes into the parent
    list, so walking ``owner.routes`` found everything.  Current FastAPI
    appends one wrapper object instead and resolves the child lazily, which
    made a flat walk stop seeing every contributed router.
    """
    keys: set[tuple[str, str]] = set()
    for route in _iter_routes(owner):
        path = getattr(route, "path", None)
        if path is None:
            continue
        for method in getattr(route, "methods", None) or set():
            keys.add((method.upper(), str(path)))
    return keys


def _iter_routes(owner: object) -> Iterator[BaseRoute]:
    """Yield every dispatchable route, descending into included routers."""
    for route in getattr(owner, "routes", None) or ():
        nested = getattr(route, "original_router", None)
        if nested is None and getattr(route, "routes", None):
            nested = route
        if nested is None:
            yield route
        else:
            yield from _iter_routes(nested)


def _remove_routes(app: FastAPI, routes: list[BaseRoute]) -> Disposer:
    return partial(_discard_routes, app, routes)


def _discard_routes(app: FastAPI, routes: list[BaseRoute]) -> bool:
    for route in routes:
        if route in app.routes:
            app.routes.remove(route)
    return True


class ServerComponent:
    """Build the HTTP/SSE FastAPI app and register it as ``ctx.server``."""

    name = "xbot.server"
    inject = ["runtime_log"]

    def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        from XBotv2.server.http import create_app

        info = ServerInfo(name="xbotv2", started_at=time.monotonic())
        app = create_app(server_name=info.name, runtime_log=ctx.runtime_log)
        carrier = WebServer(app)

        ctx.on(REGISTER_ROUTE, carrier.register_contribution)
        ctx.set("server_info", info)
        ctx.set("server", app)
        ctx.inject(["server", "server_info"], mount_core_http)


plugin = ServerComponent()
