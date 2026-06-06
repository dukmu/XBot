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
        ctx.engine.state_store.append_event(
            f"{name}_override_set",
            {"key": key, "value": value, "scope": "session"},
        )
        if name == "permission":
            rule = {"tool": key}
            if value in {"allow", "deny", "ask"}:
                ctx.engine.permission_system.add_rule(value, rule)
                getattr(ctx.engine.config, "permissions", {}).setdefault(value, []).insert(0, rule)
        else:
            sandbox = getattr(ctx.engine, "sandbox_policy", None)
            if sandbox is not None and key in {"external_read", "external_write", "workspace_read", "workspace_write"}:
                setattr(sandbox, key, _normalize_policy_value(value))
                getattr(ctx.engine.config, "sandbox", {})[key] = value
        ctx.engine.state_store.materialize()
        return _result(name, f"{name} override set for this session: {key}={value}")
    if action == "reset":
        payload = {"scope": "session"}
        if len(args) >= 2:
            payload["key"] = args[1]
        ctx.engine.state_store.append_event(f"{name}_overrides_reset", payload)
        ctx.engine.state_store.materialize()
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


def _normalize_policy_value(value: str) -> str:
    value = value.lower().strip()
    if value == "allow":
        return "readwrite"
    if value in {"readwrite", "readonly", "deny", "ask"}:
        return value
    return "ask"


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
