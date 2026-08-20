"""Server root component exposing the HTTP/SSE protocol as ``ctx.server``.

The wire protocol types and handlers stay in the protocol package; this
plugin is the dumb carrier: it builds the empty FastAPI application and
exposes it as ``ctx.server``. HTTP adapter plugins contribute routes through
the typed ``http/route`` XCore event. ``ctx.web_server`` remains temporarily
available while older adapters migrate; business state and session lifecycle
do not belong to the carrier.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, FastAPI
from starlette.routing import BaseRoute
from xcore import Disposer

from XBotv2.server.contracts import (
    REGISTER_ROUTE,
    RouteContribution,
)
from XBotv2.server.contracts import ServerInfo


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
        existing = _route_keys(self.app)
        incoming = _route_keys(router)
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

        def dispose() -> bool:
            dispose_routes()
            for error_type, handler in contribution.exception_handlers:
                if self.app.exception_handlers.get(error_type) is handler:
                    self.app.exception_handlers.pop(error_type, None)
            return True

        return dispose


def _route_keys(owner: FastAPI | APIRouter) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for route in getattr(owner, "routes", []):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        for method in methods:
            keys.add((method.upper(), str(path)))
    return keys


def _remove_routes(app: FastAPI, routes: list[BaseRoute]) -> Disposer:
    def dispose() -> bool:
        for route in routes:
            if route in app.routes:
                app.routes.remove(route)
        return True

    return dispose


class ServerComponent:
    """Build the HTTP/SSE FastAPI app and register it as ``ctx.server``."""

    name = "xbot.server"
    inject: list[str] = []

    def apply(self, ctx: Any, config: Any = None) -> None:
        from XBotv2.server.http import create_app

        info = ServerInfo(name="xbotv2", started_at=time.monotonic())
        app = create_app(server_name=info.name)
        carrier = WebServer(app)

        def register_route(contribution: RouteContribution) -> Callable[[], bool]:
            if not isinstance(contribution, RouteContribution):
                raise TypeError("http/route requires RouteContribution")
            return carrier.register_contribution(contribution)

        ctx.on(REGISTER_ROUTE, register_route)
        ctx.set("server_info", info)
        ctx.set("server", app)
        # Temporary compatibility service for callers not yet migrated. Route
        # plugins use the XCore event boundary above.
        ctx.set("web_server", carrier)


plugin = ServerComponent()
