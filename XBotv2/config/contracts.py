"""Typed policy operations owned by Settings."""

from __future__ import annotations

from dataclasses import dataclass

from XBotv2.core.operations import EmptyRequest, Operation


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    policy: dict[str, object]
    effective_permissions: dict[str, object]
    effective_sandbox: dict[str, object]


@dataclass(frozen=True, slots=True)
class PatchPolicy:
    permissions: dict[str, str] | None = None
    remove_permissions: tuple[str, ...] = ()
    sandbox: dict[str, object] | None = None
    remove_sandbox: tuple[str, ...] = ()


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
]
