"""Provide the public HTTP client as a normal client-profile capability."""

from __future__ import annotations

from XBotv2.client_transport.api.config import ClientTransportConfig
from xcore.api.context import Context
from XBotv2.client import XBotClient


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


def mount_process(ctx, config):
    ClientTransportPlugin().apply(ctx, config)


mount_process.inject = getattr(ClientTransportPlugin, "inject", ())
mount_process.provide = ('client_api',)
mount_process.Config = getattr(ClientTransportPlugin, "Config", None)
