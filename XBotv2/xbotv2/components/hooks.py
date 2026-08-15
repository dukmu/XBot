"""Hooks component: the 41-stage hook manager as a service."""

from __future__ import annotations

from typing import Any

from xbotv2.hooks.manager import HookManager


class HooksComponent:
    """Provide the bus-backed :class:`HookManager` as ``ctx.hooks``."""

    def __init__(
        self,
        *,
        plugin_runtime_factory: Any = None,
    ) -> None:
        self._plugin_runtime_factory = plugin_runtime_factory
        self.name = "xbot.hooks"

    def apply(self, ctx: Any, config: Any = None) -> None:
        hook_manager = HookManager(
            ctx, plugin_runtime_factory=self._plugin_runtime_factory
        )
        ctx.set("hooks", hook_manager)
