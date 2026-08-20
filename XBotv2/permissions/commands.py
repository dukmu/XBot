"""Human commands owned by the permissions component (``/permission``)."""

from __future__ import annotations

from XBotv2.config.contracts import PatchPolicy
from XBotv2.config.services import SettingsPort
from XBotv2.core.commands import (
    Command,
    CommandResult,
    command_usage,
    guard_command,
    run_command_operation,
    split_command_args,
)


def build_permissions_commands(settings: SettingsPort) -> tuple[Command, ...]:
    async def permission_command(raw_args: str) -> CommandResult:
        parts = split_command_args(raw_args)
        action = parts[0].lower() if parts else "status"
        if action in {"status", "list"} and len(parts) <= 1:
            value = settings.policy().effective_permissions
            return CommandResult(f"Session permission policy: {value}")
        if action == "set" and len(parts) == 3:
            tool, decision = parts[1], parts[2].lower()
            if decision not in {"allow", "deny", "ask"}:
                return CommandResult(
                    "Permission value must be allow, deny, or ask.",
                    status="error",
                )
            return await run_command_operation(
                settings.update_policy(PatchPolicy(permissions={tool: decision})),
                lambda _data: f"permission policy set: {tool}={decision}",
            )
        if action == "reset" and len(parts) == 2:
            return await run_command_operation(
                settings.update_policy(PatchPolicy(remove_permissions=(parts[1],))),
                lambda _data: "permission session policy reset.",
            )
        return command_usage(
            "/permission [status|set <tool> <decision>|reset <tool>]"
        )

    return (
        Command(
            name="permission",
            description="Inspect or update session tool permissions",
            handler=guard_command(permission_command),
            usage="/permission [status|set <tool> <decision>|reset <tool>]",
        ),
    )


__all__ = ["build_permissions_commands"]
