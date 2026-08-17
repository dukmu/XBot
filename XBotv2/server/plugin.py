"""Server root component exposing the HTTP/SSE protocol as ``ctx.server``.

The wire protocol types and handlers stay in this package; this plugin
assembles the FastAPI application (SessionManager + routes) from its tree
config and provides it as ``ctx.server``. The server app mounts the provider
directory and this host; each opened session starts its own Agent application.
"""

from __future__ import annotations

from typing import Any


class ServerComponent:
    """Build the HTTP/SSE FastAPI app and register it as ``ctx.server``."""

    name = "xbot.server"
    inject = ["llm"]

    def apply(self, ctx: Any, config: Any = None) -> None:
        from XBotv2.protocol.http_server import create_app

        config = config or {}
        app = create_app(
            paths=config["paths"],
            provider_name=config.get("provider_name", "default"),
            workspace_root=config.get("workspace_root"),
            no_plugins=bool(config.get("no_plugins", False)),
            server_name=config.get("server_name", "xbotv2"),
            llm=ctx.llm,
        )
        ctx.set("server", app)


plugin = ServerComponent()
