"""Config component: the configuration parsing service (``ctx.config``)."""

from __future__ import annotations

from typing import Any

from XBotv2.config.service import ConfigService


class ConfigComponent:
    """Register the path-bound config reader as ``ctx.settings``."""

    name = "xbot.config"

    def apply(self, ctx: Any, config: Any = None) -> None:
        ctx.set("settings", ConfigService(config["paths"]))


plugin = ConfigComponent()
