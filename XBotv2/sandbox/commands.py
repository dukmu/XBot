"""Human commands owned by the sandbox component (``/sandbox``)."""

from __future__ import annotations

import json

from XBotv2.config import PatchPolicy, SettingsPort
from XBotv2.commands import (
    Command,
    CommandResult,
    command_usage,
    guard_command,
    split_command_args,
)

_SCALAR_KEYS = (
    "enabled",
    "network",
    "external_read",
    "external_write",
    "workspace_read",
    "workspace_write",
)


def build_sandbox_commands(settings: SettingsPort) -> tuple[Command, ...]:
    async def sandbox_command(raw_args: str) -> CommandResult:
        parts = split_command_args(raw_args)
        action = parts[0].lower() if parts else "status"
        if action == "status" and len(parts) <= 1:
            snapshot = settings.policy()
            effective = snapshot.effective_sandbox
            session = snapshot.policy.get("sandbox", {})
            resources = effective.get("resources", [])
            lines = [
                "Sandbox policy",
                f"  Enforcement: {'enabled' if effective.get('enabled') else 'disabled'} (hard guard)",
                f"  Network: {'allowed' if effective.get('network') else 'isolated'}",
                "  Workspace: "
                f"read={effective.get('workspace_read')} write={effective.get('workspace_write')}",
                "  External: "
                f"read={effective.get('external_read')} write={effective.get('external_write')}",
                f"  Resources: {len(resources)} effective, "
                f"{len(session.get('resources', []))} session-local",
                "Permission approval gates escalated (sandbox-escaping) shell calls; "
                "without an active approval layer such calls fail closed.",
                "Use /sandbox resources, set, reset, add, or remove.",
            ]
            return CommandResult("\n".join(lines))
        if action in {"list", "resources"} and len(parts) == 1:
            snapshot = settings.policy()
            session_resources = snapshot.policy.get("sandbox", {}).get("resources", [])
            effective_resources = snapshot.effective_sandbox.get("resources", [])
            lines = ["Session sandbox resources (removable by index):"]
            lines.extend(
                f"  {index}. {json.dumps(resource, ensure_ascii=False, sort_keys=True)}"
                for index, resource in enumerate(session_resources, 1)
            )
            if not session_resources:
                lines.append("  none")
            inherited = effective_resources[len(session_resources):]
            if inherited:
                lines.append("Inherited resources:")
                lines.extend(
                    f"  - {json.dumps(resource, ensure_ascii=False, sort_keys=True)}"
                    for resource in inherited
                )
            return CommandResult("\n".join(lines))
        if action == "set" and len(parts) == 3:
            key, value = parts[1], parts[2].lower()
            try:
                parsed = _sandbox_value(key, value)
            except ValueError as error:
                return CommandResult(str(error), status="error")
            await settings.update_policy(PatchPolicy(sandbox={key: parsed}))
            return CommandResult(f"sandbox policy set: {key}={value}")
        if action == "add" and len(parts) == 3:
            access, path = parts[1].lower(), parts[2]
            if access not in {"allow", "deny", "readonly", "readwrite"}:
                return CommandResult(
                    "Sandbox resource access must be allow, deny, readonly, or readwrite.",
                    status="error",
                )
            snapshot = settings.policy()
            resources = list(snapshot.policy.get("sandbox", {}).get("resources", []))
            resources.insert(0, {"path": path, "access": access})
            await settings.update_policy(PatchPolicy(sandbox={"resources": resources}))
            return CommandResult(f"Added session sandbox resource: {access} {path}")
        if action == "remove" and len(parts) == 2:
            resources = list(
                settings.policy().policy.get("sandbox", {}).get("resources", [])
            )
            try:
                index = int(parts[1])
                if index < 1 or index > len(resources):
                    raise ValueError
            except ValueError:
                return CommandResult(
                    f"Resource index must be between 1 and {len(resources)}.",
                    status="error",
                )
            removed = resources.pop(index - 1)
            await settings.update_policy(PatchPolicy(sandbox={"resources": resources}))
            return CommandResult(
                "Removed session sandbox resource: "
                f"{removed.get('access', 'readonly')} {removed.get('path', '')}"
            )
        if action == "reset" and len(parts) <= 2:
            if len(parts) == 2 and parts[1] not in {*_SCALAR_KEYS, "resources"}:
                return CommandResult(
                    f"Unknown sandbox setting: {parts[1]}", status="error"
                )
            keys = (parts[1],) if len(parts) == 2 else (*_SCALAR_KEYS, "resources")
            await settings.update_policy(PatchPolicy(remove_sandbox=keys))
            return CommandResult("sandbox session policy reset.")
        return command_usage(
            "/sandbox [status|resources|set <key> <value>|reset [key]|"
            "add <access> <path>|remove <index>]"
        )

    return (
        Command(
            name="sandbox",
            effects=('policy', 'commands'),
            description="Inspect or update the session sandbox",
            handler=guard_command(sandbox_command),
            usage=(
                "/sandbox [status|resources|set <key> <value>|reset [key]|"
                "add <access> <path>|remove <index>]"
            ),
            examples=(
                "/sandbox status",
                "/sandbox add readonly /path/to/data",
                "/sandbox remove 1",
            ),
        ),
    )


def _sandbox_value(key: str, value: str) -> bool | str:
    if key in {"enabled", "network"}:
        if value in {"true", "yes", "1"}:
            return True
        if value in {"false", "no", "0"}:
            return False
        raise ValueError(f"sandbox.{key} must be true or false")
    if key not in {
        "external_read",
        "external_write",
        "workspace_read",
        "workspace_write",
    } or value not in {"allow", "deny", "readonly", "readwrite"}:
        raise ValueError(f"Invalid value {value!r} for sandbox.{key}")
    return value


__all__ = ["build_sandbox_commands"]
