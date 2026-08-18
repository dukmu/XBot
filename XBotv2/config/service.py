"""Runtime configuration service (``ctx.config``).

Provides the user context resolved by the config plugin from its tree config
and path-bound runtime config parsing for applications.  Provider
definitions are not read here — they live in the ``llm`` plugin's tree
config and are served through ``ctx.llm``.
"""

from __future__ import annotations

from typing import Any

from XBotv2.config.models import (
    RuntimeConfig,
    UserContext,
)


class ConfigService:
    """Path-bound configuration reader with a resolved user context."""

    def __init__(self, paths: Any, user_context: UserContext | None = None) -> None:
        self.paths = paths
        self._user_context = user_context or UserContext()

    def user_context(self) -> UserContext:
        return self._user_context

    def load_runtime_config(self, workspace: Any, session_id: str) -> RuntimeConfig:
        from XBotv2.config.loader import load_runtime_config

        return load_runtime_config(self.paths, workspace, session_id)

    def patch_session_policy(self, **kwargs: Any) -> dict[str, Any]:
        from XBotv2.config.policy import patch_session_policy

        return patch_session_policy(paths=self.paths, **kwargs)


__all__ = ["ConfigService"]
