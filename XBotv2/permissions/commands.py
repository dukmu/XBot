"""Human commands owned by the permissions component (``/permission``).

The handler owns its grammar and validation and uses the session policy
use case for persistence; nothing about permissions lives in the
application layer.
"""

from __future__ import annotations

from typing import Any

from XBotv2.config.loader import load_runtime_config
from XBotv2.config.policy import load_session_policy
from XBotv2.core.commands import (
    Command,
    CommandResult,
    command_usage,
    guard_command,
    run_command_operation,
    split_command_args,
)


async def permission_command(ctx: Any, raw_args: str) -> CommandResult:
    parts = split_command_args(raw_args)
    action = parts[0].lower() if parts else "status"
    if action in {"status", "list"} and len(parts) <= 1:
        config = load_runtime_config(ctx.paths, ctx.workspace_root, ctx.session_id)
        persisted = load_session_policy(ctx.paths, ctx.session_id)
        data = {
            "session_id": ctx.session_id,
            "permissions": persisted.get("permissions") or {},
            "effective_permissions": config.permissions.model_dump(),
        }
        value = data["effective_permissions"]
        return CommandResult(f"Session permission policy: {value}", data=data)
    if action == "set" and len(parts) == 3:
        tool, decision = parts[1], parts[2].lower()
        if decision not in {"allow", "deny", "ask"}:
            return CommandResult(
                "Permission value must be allow, deny, or ask.",
                status="error",
                data={"code": "invalid_value"},
            )
        return await run_command_operation(
            ctx.services.permissions.update_session_policy(
                paths=ctx.paths,
                session_id=ctx.session_id,
                contexts=[ctx],
                permissions={tool: decision},
            ),
            lambda data: f"permission policy set: {tool}={decision}",
        )
    if action == "reset" and len(parts) == 2:
        return await run_command_operation(
            ctx.services.permissions.update_session_policy(
                paths=ctx.paths,
                session_id=ctx.session_id,
                contexts=[ctx],
                remove_permissions=[parts[1]],
            ),
            lambda data: "permission session policy reset.",
        )
    return command_usage("/permission [status|set <tool> <decision>|reset [tool]]")


PERMISSIONS_COMMANDS: tuple[Command, ...] = (
    Command(
        name="permission",
        description="Inspect or update session tool permissions",
        handler=guard_command(permission_command),
        usage="/permission [status|set <tool> <decision>|reset [tool]]",
    ),
)


__all__ = ["PERMISSIONS_COMMANDS", "permission_command"]
