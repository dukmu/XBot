"""Public Settings service Protocol."""

from __future__ import annotations

from typing import Protocol

from XBotv2.config.contracts import PatchPolicy, PolicySnapshot
from XBotv2.config.models import RuntimeConfig, UserContext


class SettingsPort(Protocol):
    def user_context(self) -> UserContext: ...

    def load_runtime_config(
        self,
        workspace: object,
        session_id: str,
    ) -> RuntimeConfig: ...

    def policy(self) -> PolicySnapshot: ...

    async def update_policy(self, patch: PatchPolicy) -> PolicySnapshot: ...


__all__ = ["SettingsPort"]
