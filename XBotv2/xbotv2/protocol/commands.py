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
    parameters: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slash": self.slash,
            "kind": "server",
            "description": self.description,
            "examples": self.examples,
            "parameters": self.parameters,
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
        description="Inspect session sandbox policy.",
        examples=["/sandbox status"],
    ),
}


def list_commands(*, extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    result = [command.to_dict() for command in COMMANDS.values()]
    if extra:
        result.extend(extra)
    return result


def execute_command(ctx: Any, command: str, args: list[str], *, kind: str = "server") -> dict[str, Any]:
    command = command.lower().strip().removeprefix("/")
    if kind == "skill":
        entry = ctx.engine.tool_registry.get(command)
        if entry:
            content = entry.tool.invoke({})
            instructions = " ".join(args) if args else ""
            instruction_text = f"\n\n## Instructions\n{instructions}" if instructions else ""
            return _result(command, f"## {command}\n\n{content}{instruction_text}",
                          status="ok", data={"skill": command, "content": content})
        return _result(command, f"Skill '{command}' not found", status="error")
    if kind in ("tool", "mcp"):
        return _result(command, f"Tool '{command}' available.", data={"tool": command})
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
        return _result("provider", f"Provider switched to {provider_name} for this session.", data={"provider": provider_name})
    return _result("provider", "Usage: /provider status | list | use <name>", status="error")


def _policy_command(ctx: Any, name: str, args: list[str]) -> dict[str, Any]:
    action = args[0] if args else "status"
    if action in {"status", "list"}:
        config_key = "sandbox" if name == "sandbox" else "permissions"
        config = getattr(ctx.engine.config, config_key, {})
        overrides = getattr(ctx, f"{name}_overrides", {})
        return _result(name, f"{name} policy: {config}; session overrides: {overrides}", data={"config": config, "overrides": overrides})
    if action == "set" and len(args) >= 3:
        key, value = args[1], args[2]
        valid, normalized, message = _validate_policy_action(name, key, value)
        if not valid:
            return _result(name, message, status="error")
        getattr(ctx, f"{name}_overrides")[key] = normalized
        _reload_live_policies(ctx)
        return _result(name, f"{name} override set for this session: {key}={normalized}")
    if action == "reset":
        overrides = getattr(ctx, f"{name}_overrides")
        if len(args) >= 2:
            key = args[1]
            valid, _normalized, message = _validate_policy_action(name, key)
            if not valid:
                return _result(name, message, status="error")
            overrides.pop(key, None)
        else:
            overrides.clear()
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


def _validate_policy_action(name: str, key: str, value: str | None = None) -> tuple[bool, str, str]:
    key = key.strip()
    if not key:
        return False, "", f"/{name} requires a non-empty key."
    if value is not None:
        value = value.lower().strip()
        if name == "permission" and value not in {"allow", "deny", "ask"}:
            return False, "", "Permission value must be allow, deny, or ask."
        return True, value, ""
    return True, key, ""


def _reload_live_policies(ctx: Any) -> None:
    """Rebuild active permission/sandbox objects from config plus session overlays."""
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
    for tool, decision in getattr(ctx, "permission_overrides", {}).items():
        if decision in {"allow", "deny", "ask"}:
            permissions.setdefault(decision, []).insert(0, {"tool": tool})

    ctx.engine.config.permissions = permissions
    ctx.engine.config.sandbox = sandbox
    ctx.engine.permission_system = PermissionSystem(permissions)
    ctx.engine.sandbox_policy = SandboxPolicy(
        sandbox,
        data_root=data_dir,
        workspace_root=Path(ctx.workspace_root),
    )


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
