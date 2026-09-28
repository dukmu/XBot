"""Provide the public HTTP client as a normal client-profile capability."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from xcore import Context

from XBotv2.client import XBotClient


class ClientTransportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    base_url: str = "http://127.0.0.1:4096"
    uds_path: str | None = None


class ClientTransportPlugin:
    name = "xbot.client_transport"
    Config = ClientTransportConfig

    def apply(
        self,
        ctx: Context,
        config: ClientTransportConfig,
    ) -> None:
        client = XBotClient(config.base_url, uds_path=config.uds_path)
        ctx.set("client_api", client)
        ctx.on("dispose", client.close)


plugin = ClientTransportPlugin()

__all__ = ["ClientTransportPlugin", "plugin"]
