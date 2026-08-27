"""Runtime configuration service (``ctx.config``).

Provides the user context resolved by the config plugin from its tree config
and path-bound runtime config parsing for applications.  Provider
definitions are not read here — they live in the ``llm`` plugin's tree
config and are served through ``ctx.llm``.
"""

from __future__ import annotations

from typing import Any

from XBotv2.config.contracts import PatchPolicy, PolicySnapshot
from XBotv2.config.events import POLICY_CHANGED, PolicyChanged
from XBotv2.config.models import (
    RuntimeConfig,
    UserContext,
)
from XBotv2.core.runtime_logging import RuntimeLog


class ConfigService:
    """Path-bound configuration reader with a resolved user context."""

    def __init__(
        self,
        paths: Any,
        *,
        session_id: str,
        workspace_root: Any,
        events: Any,
        runtime_log: RuntimeLog,
        user_context: UserContext | None = None,
    ) -> None:
        self.paths = paths
        self.session_id = session_id
        self.workspace_root = workspace_root
        self.events = events
        self._log = runtime_log.bind("config", session_id=session_id)
        self._user_context = user_context or UserContext()

    def user_context(self) -> UserContext:
        return self._user_context

    def load_runtime_config(self, workspace: Any, session_id: str) -> RuntimeConfig:
        from XBotv2.config.loader import load_runtime_config

        return load_runtime_config(self.paths, workspace, session_id)

    def policy(self) -> PolicySnapshot:
        from XBotv2.config.policy import load_session_policy

        config = self.load_runtime_config(self.workspace_root, self.session_id)
        return PolicySnapshot(
            policy=load_session_policy(self.paths, self.session_id),
            effective_permissions=config.permissions.model_dump(),
            effective_sandbox=config.sandbox.model_dump(),
        )

    async def update_policy(self, patch: PatchPolicy) -> PolicySnapshot:
        from XBotv2.config.policy import patch_session_policy

        policy = patch_session_policy(
            paths=self.paths,
            session_id=self.session_id,
            permissions=patch.permissions,
            remove_permissions=patch.remove_permissions,
            sandbox=patch.sandbox,
            remove_sandbox=patch.remove_sandbox,
        )
        config = self.load_runtime_config(self.workspace_root, self.session_id)
        await self.events.emit(
            POLICY_CHANGED,
            PolicyChanged(policy=policy, config=config),
        )
        self._log.info(
            "config.policy.updated",
            permission_fields=sorted((patch.permissions or {}).keys()),
            removed_permissions=sorted(patch.remove_permissions),
            sandbox_fields=sorted((patch.sandbox or {}).keys()),
            removed_sandbox=sorted(patch.remove_sandbox),
        )
        return PolicySnapshot(
            policy=policy,
            effective_permissions=config.permissions.model_dump(),
            effective_sandbox=config.sandbox.model_dump(),
        )


__all__ = ["ConfigService"]
