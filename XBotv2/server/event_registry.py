"""Temporary capability-event inventory plugin.

This remains only while outbound events migrate to typed producer-owned XCore
events. Keeping it separate prevents the HTTP carrier from owning business
event contracts.
"""

from __future__ import annotations

from xcore import Context

from XBotv2.server.events import ServerEvents


class ServerEventRegistryComponent:
    name = "xbot.server.event-registry"

    def apply(self, ctx: Context, config: object = None) -> None:
        ctx.set("server_events", ServerEvents())


plugin = ServerEventRegistryComponent()
