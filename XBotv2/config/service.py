"""Runtime configuration service (``ctx.config``).

Reads provider / user / runtime configuration for a paths root.  Assembly-time
parsing (building the plugin tree) still uses the parse functions directly;
this service is the runtime face of the same config layer for plugins and
applications.
"""

from __future__ import annotations

from typing import Any


class ConfigService:
    """Path-bound configuration reader."""

    def __init__(self, paths: Any) -> None:
        self.paths = paths

    def provider_names(self) -> tuple[str, list[str]]:
        from XBotv2.config.loader import load_provider_names

        return load_provider_names(self.paths)

    def provider_config(self, name: str) -> Any:
        from XBotv2.config.loader import load_provider_config

        return load_provider_config(self.paths, name)

    def user_context(self) -> Any:
        from XBotv2.config.loader import load_user_context

        return load_user_context(self.paths)


__all__ = ["ConfigService"]
