"""Permissions component: the permission system as an XCore service.

Decides tool-call allow/ask/deny against the runtime permission rules and
(optionally) a parent session's permission system (subagent intersection).
"""

from __future__ import annotations

from typing import Any

from XBotv2.permissions.system import PermissionIntersection, PermissionSystem


class PermissionsComponent:
    """Register the permission system as ``ctx.permissions``."""

    name = "xbot.permissions"

    def apply(self, ctx: Any, config: Any = None) -> None:
        runtime_config = ctx.runtime
        permissions = PermissionSystem(
            runtime_config.permissions,
            variables=ctx.variables,
        )
        parent = (config or {}).get("parent_permission_system")
        if parent is not None:
            permissions = PermissionIntersection(parent, permissions)
        ctx.set("permissions", permissions)


plugin = PermissionsComponent()
