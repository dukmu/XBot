"""Server-side command registry for TUI discovery and execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ServerCommand:
    name: str
    slash: str
    description: str
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slash": self.slash,
            "description": self.description,
            "scope": "server",
            "examples": self.examples,
        }


COMMANDS: dict[str, ServerCommand] = {
    "status": ServerCommand(
        name="status",
        slash="/status",
        description="Show server and current session status.",
        examples=["/status"],
    ),
    "provider": ServerCommand(
        name="provider",
        slash="/provider",
        description="List or switch provider configuration.",
        examples=["/provider list", "/provider status", "/provider use deepseek"],
    ),
    "permission": ServerCommand(
        name="permission",
        slash="/permission",
        description="Inspect or update session permission policy.",
        examples=["/permission status", "/permission set shell allow"],
    ),
    "sandbox": ServerCommand(
        name="sandbox",
        slash="/sandbox",
        description="Inspect or update session sandbox policy.",
        examples=["/sandbox status", "/sandbox set external_read ask"],
    ),
}


def list_commands() -> list[dict[str, Any]]:
    return [command.to_dict() for command in COMMANDS.values()]


def execute_command(ctx: Any, command: str, args: list[str]) -> dict[str, Any]:
    command = command.lower().strip().removeprefix("/")
    if command == "status":
        return _result("status", _status_message(ctx), data=_status_data(ctx))
    if command == "provider":
        return _provider_command(ctx, args)
    if command == "permission":
        return _policy_command(ctx, "permission", args)
    if command == "sandbox":
        return _policy_command(ctx, "sandbox", args)
    return _result(command, f"Unknown server command: /{command}", status="error")


def _provider_command(ctx: Any, args: list[str]) -> dict[str, Any]:
    action = args[0] if args else "status"
    if action == "status":
        return _result("provider", f"Provider: {ctx.provider_name}", data={"provider": ctx.provider_name})
    if action == "list":
        from xbotv2.config.loader import load_provider_names

        default, names = load_provider_names(Path(ctx.data_dir))
        current = ctx.provider_name
        if not names:
            return _result("provider", "No providers configured.", data={"default": default, "providers": []})
        message = "Providers: " + ", ".join(
            f"{name}{' (current)' if name == current else ''}{' (default)' if name == default else ''}"
            for name in names
        )
        return _result("provider", message, data={"default": default, "providers": names, "current": current})
    if action == "use" and len(args) >= 2:
        if ctx.turn_lock.locked():
            return _result("provider", "Cannot switch provider while a turn is active.", status="error")
        from xbotv2.config.loader import load_provider_config, load_provider_names
        from xbotv2.llm.client import create_llm

        provider_name = args[1]
        _default, names = load_provider_names(Path(ctx.data_dir))
        if provider_name not in names:
            return _result("provider", f"Unknown provider: {provider_name}", status="error")
        provider_config = load_provider_config(Path(ctx.data_dir), provider_name)
        ctx.engine.llm = create_llm(provider_config)
        ctx.provider_name = provider_name
        if hasattr(ctx.engine.config, "provider"):
            ctx.engine.config.provider = provider_name
        if hasattr(ctx.engine.state_store, "provider"):
            ctx.engine.state_store.provider = provider_name
        ctx.engine.state_store.append_event(
            "provider_switched",
            {"provider": provider_name, "scope": "session"},
        )
        ctx.engine.state_store.materialize()
        return _result("provider", f"Provider switched to {provider_name} for this session.", data={"provider": provider_name})
    return _result("provider", "Usage: /provider status | list | use <name>", status="error")


def _policy_command(ctx: Any, name: str, args: list[str]) -> dict[str, Any]:
    action = args[0] if args else "status"
    if action in {"status", "list"}:
        config_key = "sandbox" if name == "sandbox" else "permissions"
        config = getattr(ctx.engine.config, config_key, {})
        overrides = ctx.engine.state_store.read_state().get(f"{name}_overrides", {})
        return _result(name, f"{name} policy: {config}; session overrides: {overrides}", data={"config": config, "overrides": overrides})
    if action == "set" and len(args) >= 3:
        key, value = args[1], args[2]
        valid, normalized, message = _validate_policy_override(name, key, value)
        if not valid:
            return _result(name, message, status="error")
        ctx.engine.state_store.append_event(
            f"{name}_override_set",
            {"key": key, "value": normalized, "scope": "session"},
        )
        ctx.engine.state_store.materialize()
        _reload_live_policies(ctx)
        return _result(name, f"{name} override set for this session: {key}={normalized}")
    if action == "reset":
        payload = {"scope": "session"}
        if len(args) >= 2:
            key = args[1]
            valid, _normalized, message = _validate_policy_reset(name, key)
            if not valid:
                return _result(name, message, status="error")
            payload["key"] = key
        ctx.engine.state_store.append_event(f"{name}_overrides_reset", payload)
        ctx.engine.state_store.materialize()
        _reload_live_policies(ctx)
        return _result(name, f"{name} session overrides reset.")
    return _result(name, f"Usage: /{name} status | set <key> <value> | reset", status="error")


def _status_data(ctx: Any) -> dict[str, Any]:
    return {
        "session_id": ctx.session_id,
        "thread_id": ctx.thread_id,
        "workspace_root": ctx.workspace_root,
        "provider": ctx.provider_name,
        "turn_active": ctx.turn_lock.locked(),
    }


def _validate_policy_override(name: str, key: str, value: str) -> tuple[bool, str, str]:
    key = key.strip()
    value = value.lower().strip()
    if not key:
        return False, "", f"/{name} set requires a non-empty key."
    if name == "permission":
        if value not in {"allow", "deny", "ask"}:
            return False, "", "Permission value must be allow, deny, or ask."
        return True, value, ""
    if key not in _SANDBOX_KEYS:
        return False, "", "Sandbox key must be external_read, external_write, workspace_read, or workspace_write."
    if value not in _SANDBOX_VALUES:
        return False, "", "Sandbox value must be allow, readwrite, readonly, deny, or ask."
    return True, value, ""


def _validate_policy_reset(name: str, key: str) -> tuple[bool, str, str]:
    key = key.strip()
    if not key:
        return False, "", f"/{name} reset key must be non-empty."
    if name == "sandbox" and key not in _SANDBOX_KEYS:
        return False, "", "Sandbox key must be external_read, external_write, workspace_read, or workspace_write."
    return True, key, ""


def _reload_live_policies(ctx: Any) -> None:
    """Rebuild active permission/sandbox objects from config plus command events."""
    from xbotv2.config.loader import load_system_config
    from xbotv2.config.policy import (
        load_session_policy,
        merge_permission_config,
        merge_sandbox_config,
    )
    from xbotv2.tools.permissions import PermissionSystem
    from xbotv2.tools.sandbox import SandboxPolicy

    data_dir = Path(ctx.data_dir)
    base_config = load_system_config(data_dir, Path(ctx.workspace_root))
    session_policy = load_session_policy(data_dir, ctx.session_id)
    permissions = merge_permission_config(
        base_config.permissions,
        session_policy.get("permissions"),
    )
    sandbox = merge_sandbox_config(
        base_config.sandbox,
        session_policy.get("sandbox"),
    )
    state = ctx.engine.state_store.read_state()
    for tool, decision in state.get("permission_overrides", {}).items():
        if decision in {"allow", "deny", "ask"}:
            permissions.setdefault(decision, []).insert(0, {"tool": tool})
    for key, value in state.get("sandbox_overrides", {}).items():
        if key in _SANDBOX_KEYS and value in _SANDBOX_VALUES:
            sandbox[key] = value

    ctx.engine.config.permissions = permissions
    ctx.engine.config.sandbox = sandbox
    ctx.engine.permission_system = PermissionSystem(permissions)
    ctx.engine.sandbox_policy = SandboxPolicy(
        sandbox,
        data_root=data_dir,
        workspace_root=Path(ctx.workspace_root),
    )


_SANDBOX_KEYS = {"external_read", "external_write", "workspace_read", "workspace_write"}
_SANDBOX_VALUES = {"allow", "readwrite", "readonly", "deny", "ask"}


def _status_message(ctx: Any) -> str:
    data = _status_data(ctx)
    return (
        f"session={data['session_id']} thread={data['thread_id']} "
        f"provider={data['provider']} workspace={data['workspace_root']}"
    )


def _result(command: str, message: str, *, status: str = "ok", data: Any = None) -> dict[str, Any]:
    return {
        "type": "command_result",
        "data": {
            "command": command,
            "status": status,
            "message": message,
            "data": data,
        },
    }
