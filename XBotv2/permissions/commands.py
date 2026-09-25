"""Human commands owned by the permissions component (``/permission``)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from XBotv2.config import PatchPolicy, SettingsPort
from XBotv2.permissions.contracts import PermissionRule
from XBotv2.commands import (
    Command,
    CommandResult,
    command_usage,
    guard_command,
    split_command_args,
)

if TYPE_CHECKING:
    from XBotv2.permissions.plugin import PermissionsService


def build_permissions_commands(
    settings: SettingsPort,
    permissions: "PermissionsService",
) -> tuple[Command, ...]:
    async def permission_command(raw_args: str) -> CommandResult:
        parts = split_command_args(raw_args)
        action = parts[0].lower() if parts else "status"
        if action == "status" and len(parts) <= 1:
            snapshot = settings.policy()
            policies = settings.permission_policies()
            effective = tuple(rule for policy in policies for rule in policy.rules)
            session = snapshot.policy.get("permissions", {})
            grants = permissions.session_grants()
            lines = [
                "Permission policy",
                "  Effective layers: " + str(len(policies)),
                "  Effective rules: " + ", ".join(
                    f"{decision}={sum(rule.decision == decision for rule in effective)}"
                    for decision in ("deny", "allow", "ask")
                ),
                f"  Session overrides: {len(session.get('rules', []))} rule(s)",
                f"  Approved grants: {len(grants)} (persisted for this Agent thread)",
                "  Precedence: deny > grant > allow > ask > default ask",
                "Use /permission list, rules, grants, set, reset, revoke, or clear-grants.",
            ]
            return CommandResult("\n".join(lines))
        if action in {"list", "rules"} and len(parts) == 1:
            snapshot = settings.policy()
            policies = settings.permission_policies()
            session = snapshot.policy.get("permissions", {})
            lines = ["Effective permission rules:"]
            for index, policy in enumerate(policies, 1):
                lines.append(
                    f"  layer {index} (default={policy.default_decision}):"
                )
                lines.extend(
                    "    - " + json.dumps(
                        rule.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    for rule in policy.rules
                )
            lines.append("Session policy overrides:")
            if session:
                for rule in session.get("rules", []):
                    lines.append(
                        "  " + json.dumps(rule, ensure_ascii=False, sort_keys=True)
                    )
            else:
                lines.append("  none")
            if action == "rules":
                return CommandResult("\n".join(lines))
            lines.append("")
            lines.extend(_grant_lines(permissions.session_grants()))
            return CommandResult("\n".join(lines))
        if action == "grants" and len(parts) == 1:
            grants = permissions.session_grants()
            return CommandResult("\n".join(_grant_lines(grants)))
        if action == "set" and len(parts) == 3:
            tool, decision = parts[1], parts[2].lower()
            if decision not in {"allow", "deny", "ask"}:
                return CommandResult(
                    "Permission value must be allow, deny, or ask.",
                    status="error",
                )
            await settings.update_policy(PatchPolicy(permissions={tool: decision}))
            return CommandResult(f"permission policy set: {tool}={decision}")
        if action == "reset" and len(parts) == 2:
            await settings.update_policy(
                PatchPolicy(remove_permissions=(parts[1],))
            )
            return CommandResult("permission session policy reset.")
        if action == "revoke" and len(parts) == 2:
            try:
                index = int(parts[1])
                await permissions.revoke_session(index)
            except ValueError as error:
                if str(error).startswith("Grant index must be between"):
                    return CommandResult(str(error), status="error")
                return CommandResult("Grant index must be a positive integer.", status="error")
            return CommandResult(f"Revoked approved grant {index}.")
        if action == "clear-grants" and len(parts) == 1:
            count = await permissions.clear_session_grants()
            return CommandResult(f"Cleared {count} approved session grant(s).")
        return command_usage(
            "/permission [status|list|rules|grants|set <tool> <decision>|"
            "reset <tool>|revoke <index>|clear-grants]"
        )

    return (
        Command(
            name="permission",
            effects=('policy', 'commands'),
            description="Inspect or update session tool permissions",
            handler=guard_command(permission_command),
            usage=(
                "/permission [status|list|rules|grants|set <tool> <decision>|"
                "reset <tool>|revoke <index>|clear-grants]"
            ),
            examples=(
                "/permission status",
                "/permission rules",
                "/permission grants",
                "/permission revoke 1",
            ),
        ),
    )


def _grant_lines(grants: tuple[PermissionRule, ...]) -> list[str]:
    lines = ["Approved session grants (removable by index):"]
    lines.extend(
        f"  {index}. "
        f"{json.dumps(rule.model_dump(mode='json', exclude_none=True), ensure_ascii=False, sort_keys=True)}"
        for index, rule in enumerate(grants, 1)
    )
    if not grants:
        lines.append("  none")
    return lines


__all__ = ["build_permissions_commands"]
