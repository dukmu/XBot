"""Runtime configuration service (``ctx.config``).

Provides the user context resolved by the config plugin from its tree config
and path-bound runtime config parsing for applications.  Provider
definitions are not read here — they live in the ``llm`` plugin's tree
config and are served through ``ctx.llm``.
"""

from __future__ import annotations

from typing import Any

from XBotv2.config.models import UserContext


class ConfigService:
    """Path-bound configuration reader with a resolved user context."""

    def __init__(self, paths: Any, user_context: UserContext | None = None) -> None:
        self.paths = paths
        self._user_context = user_context or UserContext()

    def user_context(self) -> UserContext:
        return self._user_context


__all__ = ["ConfigService"]
