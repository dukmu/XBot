"""Human commands owned by the sandbox component (``/sandbox``)."""

from __future__ import annotations

from XBotv2.config.contracts import PatchPolicy
from XBotv2.config.services import SettingsPort
from XBotv2.commands import (
    Command,
    CommandResult,
    command_usage,
    guard_command,
    run_command_operation,
    split_command_args,
)


def build_sandbox_commands(settings: SettingsPort) -> tuple[Command, ...]:
    async def sandbox_command(raw_args: str) -> CommandResult:
        parts = split_command_args(raw_args)
        action = parts[0].lower() if parts else "status"
        if action in {"status", "list"} and len(parts) <= 1:
            value = settings.policy().effective_sandbox
            return CommandResult(f"Session sandbox policy: {value}")
        if action == "set" and len(parts) == 3:
            key, value = parts[1], parts[2].lower()
            try:
                parsed = _sandbox_value(key, value)
            except ValueError as error:
                return CommandResult(str(error), status="error")
            return await run_command_operation(
                settings.update_policy(PatchPolicy(sandbox={key: parsed})),
                lambda _data: f"sandbox policy set: {key}={value}",
            )
        if action == "reset" and len(parts) <= 2:
            keys = (
                (parts[1],)
                if len(parts) == 2
                else (
                    "enabled",
                    "network",
                    "external_read",
                    "external_write",
                    "workspace_read",
                    "workspace_write",
                )
            )
            return await run_command_operation(
                settings.update_policy(PatchPolicy(remove_sandbox=keys)),
                lambda _data: "sandbox session policy reset.",
            )
        return command_usage("/sandbox [status|set <key> <value>|reset [key]]")

    return (
        Command(
            name="sandbox",
            description="Inspect or update the session sandbox",
            handler=guard_command(sandbox_command),
            usage="/sandbox [status|set <key> <value>|reset [key]]",
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
    } or value not in {"allow", "deny", "ask", "readonly", "readwrite"}:
        raise ValueError(f"Invalid value {value!r} for sandbox.{key}")
    return value


__all__ = ["build_sandbox_commands"]
