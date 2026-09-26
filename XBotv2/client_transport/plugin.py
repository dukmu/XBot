"""Provide the public HTTP client as a normal client-profile capability."""

from __future__ import annotations

from pydantic import JsonValue
from xcore import Context

from XBotv2.application.client import ClientLaunch
from XBotv2.client import XBotClient


class ClientTransportPlugin:
    name = "xbot.client_transport"
    inject = ["client_launch"]

    async def apply(
        self,
        ctx: Context,
        config: dict[str, JsonValue] | None = None,
    ) -> None:
        launch: ClientLaunch = ctx.require("client_launch")
        client = XBotClient(launch.base_url, uds_path=launch.uds_path)
        ctx.set("client_api", client)
        ctx.on("dispose", client.close)


plugin = ClientTransportPlugin()

__all__ = ["ClientTransportPlugin", "plugin"]
