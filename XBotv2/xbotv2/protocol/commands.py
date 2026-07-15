"""Server-side command registry for TUI discovery and execution."""

from __future__ import annotations

import secrets
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from xbotv2.api.commands import Command

CommandHandler = Callable[[Any, list[str]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ServerCommand:
    name: str
    slash: str
    description: str
    handler: CommandHandler
    examples: list[str] = field(default_factory=list)
    parameters: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slash": self.slash,
            "kind": "server",
            "description": self.description,
            "usage": self.slash,
            "examples": self.examples,
            "parameters": self.parameters,
        }


def list_commands(*, extra: tuple[Command, ...] = ()) -> list[dict[str, Any]]:
    result = [command.to_dict() for command in COMMANDS.values()]
    registered = set(COMMANDS)
    result.extend(
        _plugin_command_dict(command)
        for command in extra
        if command.name not in registered
    )
    return result


async def execute_command(
    ctx: Any,
    command: str,
    args: list[str],
    *,
    kind: str = "server",
    raw_args: str = "",
) -> dict[str, Any]:
    command = command.lower().strip().removeprefix("/")
    registered = COMMANDS.get(command)
    if registered is not None:
        return await registered.handler(ctx, args)
    if kind == "prompt":
        return _result(
            command,
            "Prompt expansions must be submitted through the message endpoint.",
            status="error",
        )
    loader = getattr(ctx.engine, "plugin_loader", None)
    extension = loader.get_command(command) if loader is not None else None
    if extension is None or extension.kind != "server":
        return _result(command, f"Unknown server command: /{command}", status="error")
    assert extension.handler is not None
    result = await extension.handler(ctx, raw_args)
    return _result(
        command,
        result.message,
        status=result.status,
        data=result.data,
        history=result.history,
    )


def _plugin_command_dict(command: Command) -> dict[str, Any]:
    return {
        "name": command.name,
        "slash": f"/{command.name}",
        "kind": command.kind,
        "description": command.description,
        "usage": command.usage or f"/{command.name}",
        "examples": list(command.examples),
        "parameters": command.parameters,
    }


async def _clear_command(ctx: Any, args: list[str]) -> dict[str, Any]:
    if ctx.turn_lock.locked():
        return _result(
            "clear",
            "Cannot rewrite history while a turn is active.",
            status="error",
        )
    if args:
        return _result("clear", "Usage: /clear", status="error")
    removed_turns = sum(
        message.role == "user" for message in ctx.engine.messages
    )
    await ctx.engine.replace_history([])
    return _result(
        "clear",
        f"Cleared {removed_turns} conversation turns.",
        data={"removed_turns": removed_turns},
        history=[],
    )


async def _undo_command(ctx: Any, args: list[str]) -> dict[str, Any]:
    if ctx.turn_lock.locked():
        return _result(
            "undo",
            "Cannot rewrite history while a turn is active.",
            status="error",
        )
    if len(args) > 1:
        return _result("undo", "Usage: /undo [count]", status="error")
    try:
        count = int(args[0]) if args else 1
    except ValueError:
        return _result("undo", "Undo count must be a positive integer.", status="error")
    messages = list(ctx.engine.messages)
    user_indexes = [
        index for index, message in enumerate(messages) if message.role == "user"
    ]
    if count < 1:
        return _result("undo", "Undo count must be a positive integer.", status="error")
    if count > len(user_indexes):
        return _result(
            "undo",
            f"Cannot undo {count} turns; session has {len(user_indexes)}.",
            status="error",
        )
    kept = messages[:user_indexes[-count]]
    await ctx.engine.replace_history(kept)
    return _result(
        "undo",
        f"Removed {count} conversation turn{'s' if count != 1 else ''}.",
        data={"removed_turns": count},
        history=_display_history(kept),
    )


async def _fork_command(ctx: Any, args: list[str]) -> dict[str, Any]:
    if args:
        return _result("fork", "Usage: /fork", status="error")
    if ctx.turn_lock.locked():
        return _result("fork", "Cannot fork while a turn is active.", status="error")
    await ctx.engine.save_messages()
    session_id = _new_fork_id()
    while ctx.paths.session(session_id).root.exists():
        session_id = _new_fork_id()
    source = ctx.paths.session(ctx.session_id).root
    target = ctx.paths.session(session_id).root
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return _result(
        "fork",
        f"Forked session {ctx.session_id} to {session_id}.",
        data={"session_id": session_id, "source_session_id": ctx.session_id},
    )


def _background_tasks(ctx: Any) -> Any | None:
    return getattr(ctx.engine, "background_tasks", None)


async def _tasks_command(ctx: Any, args: list[str]) -> dict[str, Any]:
    if args not in ([], ["ps"]):
        return _result("tasks", "Usage: /tasks [ps]", status="error")
    manager = _background_tasks(ctx)
    if manager is None:
        return _result("tasks", "Background tasks are unavailable.", status="error")
    tasks = manager.snapshots()
    if not tasks:
        return _result("tasks", "No background tasks.", data={"tasks": []})
    lines = [
        f"{task['task_id']}  {task['status']}  {task['command']}"
        for task in tasks
    ]
    return _result("tasks", "\n".join(lines), data={"tasks": tasks})


async def _task_command(ctx: Any, args: list[str]) -> dict[str, Any]:
    manager = _background_tasks(ctx)
    if manager is None:
        return _result("task", "Background tasks are unavailable.", status="error")
    if len(args) == 2 and args[0] == "stop":
        result = await manager.stop_task(args[1])
        return _result(
            "task",
            str(result.content),
            status="ok" if result.status == "success" else "error",
            data=result.data,
        )
    if args == ["stopall"]:
        stopped = await manager.stop_all()
        return _result(
            "task",
            f"Stopped {len(stopped)} background task(s).",
            data={"tasks": stopped},
        )
    return _result(
        "task",
        "Usage: /task stop <id> | /task stopall",
        status="error",
    )


def _new_fork_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def _display_history(messages: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "role": message.role,
            "content": str(message.content or ""),
            "tool_calls": [call.to_dict() for call in message.tool_calls or []],
            "tool_call_id": message.tool_call_id,
            "status": message.status,
        }
        for message in messages
        if message.role in {"user", "assistant", "tool"}
    ]


async def _provider_command(ctx: Any, args: list[str]) -> dict[str, Any]:
    action = args[0] if args else "status"
    if action == "status":
        model = str(getattr(ctx.engine, "model", ""))
        return _result(
            "provider",
            f"Provider: {ctx.provider_name}" + (f" ({model})" if model else ""),
            data={"provider": ctx.provider_name, "model": model},
        )
    if action == "list":
        from xbotv2.config.loader import load_provider_names

        default, names = load_provider_names(ctx.paths)
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
        _default, names = load_provider_names(ctx.paths)
        if provider_name not in names:
            return _result("provider", f"Unknown provider: {provider_name}", status="error")
        provider_config = load_provider_config(ctx.paths, provider_name)
        ctx.engine.llm = create_llm(provider_config)
        ctx.engine.model = provider_config.model
        ctx.provider_name = provider_name
        if hasattr(ctx.engine.config, "provider"):
            ctx.engine.config.provider = provider_name
        if hasattr(ctx.engine.state_store, "provider"):
            ctx.engine.state_store.provider = provider_name
        return _result(
            "provider",
            f"Provider switched to {provider_name} ({provider_config.model}) for this session.",
            data={"provider": provider_name, "model": provider_config.model},
        )
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
        if name == "sandbox":
            _persist_sandbox_overrides(ctx, key, normalized)
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
            if name == "sandbox":
                _remove_sandbox_override(ctx, key)
        else:
            overrides.clear()
            if name == "sandbox":
                _clear_sandbox_overrides(ctx)
        _reload_live_policies(ctx)
        return _result(name, f"{name} session overrides reset.")
    return _result(name, f"Usage: /{name} status | set <key> <value> | reset", status="error")


async def _permission_command(ctx: Any, args: list[str]) -> dict[str, Any]:
    return _policy_command(ctx, "permission", args)


async def _sandbox_command(ctx: Any, args: list[str]) -> dict[str, Any]:
    return _policy_command(ctx, "sandbox", args)


async def _status_command(ctx: Any, args: list[str]) -> dict[str, Any]:
    if args:
        return _result("status", "Usage: /status", status="error")
    return _result("status", _status_message(ctx), data=_status_data(ctx))


def _status_data(ctx: Any) -> dict[str, Any]:
    loader = getattr(ctx.engine, "plugin_loader", None)
    return {
        "session_id": ctx.session_id,
        "thread_id": ctx.thread_id,
        "workspace_root": ctx.workspace_root,
        "provider": ctx.provider_name,
        "model": str(getattr(ctx.engine, "model", "")),
        "context_window": int(getattr(ctx.engine, "context_window", 0)),
        "turn_active": ctx.turn_lock.locked(),
        "plugins": loader.diagnostics() if loader is not None else [],
    }


def _validate_policy_action(name: str, key: str, value: str | None = None) -> tuple[bool, str, str]:
    key = key.strip()
    if not key:
        return False, "", f"/{name} requires a non-empty key."
    if value is not None:
        value = value.lower().strip()
        if name == "permission" and value not in {"allow", "deny", "ask"}:
            return False, "", "Permission value must be allow, deny, or ask."
        if name == "sandbox":
            normalized = _normalize_sandbox_value(key, value)
            if normalized is None:
                return False, "", f"Invalid value {value!r} for sandbox.{key}"
            return True, normalized, ""
        return True, value, ""
    return True, key, ""


def _normalize_sandbox_value(key: str, value: str) -> str | bool | None:
    """Coerce a user-supplied sandbox value to the correct Python type."""
    special = {
        "enabled": "bool",
        "network": "bool",
        "external_read": "str",
        "external_write": "str",
        "workspace_read": "str",
        "workspace_write": "str",
    }
    kind = special.get(key)
    if kind == "bool":
        if value in {"true", "yes", "1"}:
            return True
        if value in {"false", "no", "0"}:
            return False
        return None
    if kind == "str" and value in {"ask", "deny", "allow", "readonly", "readwrite"}:
        return value
    return value if key not in special else None


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

    base_config = load_system_config(ctx.paths, Path(ctx.workspace_root))
    session_policy = load_session_policy(ctx.paths, ctx.session_id)
    permissions = merge_permission_config(
        base_config.permissions,
        session_policy.get("permissions"),
    )
    for tool, decision in getattr(ctx, "permission_overrides", {}).items():
        if decision in {"allow", "deny", "ask"}:
            permissions.setdefault(decision, []).insert(0, {"tool": tool})

    sandbox = merge_sandbox_config(
        base_config.sandbox,
        session_policy.get("sandbox"),
        overrides=getattr(ctx, "sandbox_overrides", None),
    )

    ctx.engine.config.permissions = permissions
    ctx.engine.config.sandbox = sandbox
    ctx.engine.permission_system = PermissionSystem(permissions)
    sandbox_policy = SandboxPolicy(
        sandbox,
        data_root=ctx.paths.data_dir,
        workspace_root=Path(ctx.workspace_root),
        session_root=ctx.engine.sandbox_policy.session_root,
    )
    ctx.engine.sandbox_policy = sandbox_policy


def _persist_sandbox_overrides(ctx: Any, key: str, value: Any) -> None:
    overrides = getattr(ctx, "sandbox_overrides", {})
    if not overrides:
        return
    from xbotv2.config.policy import persist_sandbox_config
    try:
        persist_sandbox_config(
            paths=ctx.paths,
            session_id=ctx.session_id,
            sandbox=dict(overrides),
        )
    except Exception as exc:
        from xbotv2.protocol.http_server import logger
        logger.warning("sandbox override persist failed: %s", exc)


def _remove_sandbox_override(ctx: Any, key: str) -> None:
    overrides = getattr(ctx, "sandbox_overrides", {})
    if overrides:
        _persist_sandbox_overrides(ctx, key, None)


def _clear_sandbox_overrides(ctx: Any) -> None:
    from xbotv2.config.policy import clear_sandbox_config
    try:
        clear_sandbox_config(
            paths=ctx.paths,
            session_id=ctx.session_id,
        )
    except Exception as exc:
        from xbotv2.protocol.http_server import logger
        logger.warning("sandbox override clear failed: %s", exc)


def _status_message(ctx: Any) -> str:
    data = _status_data(ctx)
    return (
        f"session={data['session_id']} thread={data['thread_id']} "
        f"provider={data['provider']} workspace={data['workspace_root']}"
    )


def _result(
    command: str,
    message: str,
    *,
    status: str = "ok",
    data: Any = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "type": "command_result",
        "data": {
            "command": command,
            "status": status,
            "message": message,
            "data": data,
            "history": history,
        },
    }


COMMANDS: dict[str, ServerCommand] = {
    command.name: command
    for command in (
        ServerCommand(
            name="status",
            slash="/status",
            description="Show server and current session status.",
            handler=_status_command,
            examples=["/status"],
        ),
        ServerCommand(
            name="provider",
            slash="/provider",
            description="List or switch provider configuration.",
            handler=_provider_command,
            examples=[
                "/provider list",
                "/provider status",
                "/provider use deepseek",
            ],
        ),
        ServerCommand(
            name="permission",
            slash="/permission",
            description="Inspect or update session permission policy.",
            handler=_permission_command,
            examples=["/permission status", "/permission set shell allow"],
        ),
        ServerCommand(
            name="sandbox",
            slash="/sandbox",
            description="Inspect session sandbox policy.",
            handler=_sandbox_command,
            examples=["/sandbox status"],
        ),
        ServerCommand(
            name="fork",
            slash="/fork",
            description="Fork persisted session state into a new session.",
            handler=_fork_command,
            examples=["/fork"],
        ),
        ServerCommand(
            name="clear",
            slash="/clear",
            description="Clear conversation history while preserving session state.",
            handler=_clear_command,
            examples=["/clear"],
        ),
        ServerCommand(
            name="undo",
            slash="/undo",
            description="Remove the most recent complete conversation turns.",
            handler=_undo_command,
            examples=["/undo", "/undo 2"],
            parameters={"count": "Number of turns to remove; defaults to 1."},
        ),
        ServerCommand(
            name="tasks",
            slash="/tasks [ps]",
            description="List background shell tasks.",
            handler=_tasks_command,
            examples=["/tasks", "/tasks ps"],
        ),
        ServerCommand(
            name="task",
            slash="/task stop <id> | /task stopall",
            description="Stop background shell tasks.",
            handler=_task_command,
            examples=["/task stop task-1", "/task stopall"],
            parameters={"action": "stop <id> or stopall"},
        ),
    )
}
