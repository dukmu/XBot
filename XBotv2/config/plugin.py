"""Config component: the configuration parsing service (``ctx.config``)."""

from __future__ import annotations

from typing import Any

from XBotv2.config.models import UserContext
from XBotv2.config.service import ConfigService


class ConfigComponent:
    """Register the path-bound config reader as ``ctx.settings``.

    The user context comes from this plugin's tree config (``user`` block),
    not a separate ``user.yaml`` document — consistent with the plugin-tree
    configuration model (``xcore.yaml`` + overlays).
    """

    name = "xbot.config"

    def apply(self, ctx: Any, config: Any = None) -> None:
        config = config or {}
        user = UserContext.model_validate(config.get("user") or {})
        ctx.set("settings", ConfigService(config["paths"], user_context=user))


plugin = ConfigComponent()
