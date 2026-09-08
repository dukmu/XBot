"""Typed policy operations owned by Settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from pydantic import JsonValue

from XBotv2.config.models import RuntimeConfig, UserContext
from XBotv2.core.operations import EmptyRequest, Operation


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    policy: dict[str, JsonValue]
    effective_permissions: dict[str, JsonValue]
    effective_sandbox: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class PatchPolicy:
    permissions: dict[str, str] | None = None
    remove_permissions: tuple[str, ...] = ()
    sandbox: dict[str, JsonValue] | None = None
    remove_sandbox: tuple[str, ...] = ()


class SettingsPort(Protocol):
    """Session-bound configuration service mounted as ``ctx.settings``."""

    def user_context(self) -> UserContext: ...
    def load_runtime_config(
        self,
        workspace: Path,
        session_id: str,
    ) -> RuntimeConfig: ...
    def policy(self) -> PolicySnapshot: ...
    async def update_policy(self, patch: PatchPolicy) -> PolicySnapshot: ...


GET_POLICY = Operation("config/policy/get", EmptyRequest, PolicySnapshot)
UPDATE_POLICY = Operation(
    "config/policy/update",
    PatchPolicy,
    PolicySnapshot,
    exclusive=True,
)


__all__ = [
    "GET_POLICY",
    "UPDATE_POLICY",
    "PatchPolicy",
    "PolicySnapshot",
    "SettingsPort",
]
