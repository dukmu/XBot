"""Protocol component: the HTTP/SSE server as a plugin (``ctx.server``).

The wire protocol types and handlers stay in this package; this plugin
assembles the FastAPI application (SessionManager + routes) from its tree
config and provides it as ``ctx.server``.  A server-style root mounts this
entry via the composition root's ``extra_plugins`` (with the agent loop
excluded); each session the server opens bootstraps its own full runtime.
"""

from __future__ import annotations

from typing import Any


class ServerComponent:
    """Build the HTTP/SSE FastAPI app and register it as ``ctx.server``."""

    name = "xbot.protocol"

    def apply(self, ctx: Any, config: Any = None) -> None:
        from XBotv2.protocol.http_server import create_app

        config = config or {}
        app = create_app(
            paths=config["paths"],
            provider_name=config.get("provider_name", "default"),
            workspace_root=config.get("workspace_root"),
            no_plugins=bool(config.get("no_plugins", False)),
            server_name=config.get("server_name", "xbotv2"),
            llm=getattr(ctx, "llm", None),
        )
        ctx.set("server", app)


plugin = ServerComponent()
