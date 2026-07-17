"""Server-side command registry for TUI discovery and execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from xbotv2.api.commands import Command
from xbotv2.protocol.runtime_operations import (
    OperationError,
    clear_history,
    fork_session,
    reload_live_policies,
    select_agent,
    select_provider,
    stop_all_tasks,
    stop_task,
    task_snapshots,
    undo_history,
)

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
    if args:
        return _result("clear", "Usage: /clear", status="error")
    try:
        removed_turns = await clear_history(ctx)
    except OperationError as exc:
        return _operation_error("clear", exc)
    return _result(
        "clear",
        f"Cleared {removed_turns} conversation turns.",
        data={"removed_turns": removed_turns},
        history=[],
    )


async def _undo_command(ctx: Any, args: list[str]) -> dict[str, Any]:
    if len(args) > 1:
        return _result("undo", "Usage: /undo [count]", status="error")
    try:
        count = int(args[0]) if args else 1
    except ValueError:
        return _result("undo", "Undo count must be a positive integer.", status="error")
    try:
        kept = await undo_history(ctx, count)
    except OperationError as exc:
        return _operation_error("undo", exc)
    return _result(
        "undo",
        f"Removed {count} conversation turn{'s' if count != 1 else ''}.",
        data={"removed_turns": count},
        history=_display_history(kept),
    )


async def _fork_command(ctx: Any, args: list[str]) -> dict[str, Any]:
    if args:
        return _result("fork", "Usage: /fork", status="error")
    try:
        session_id = await fork_session(ctx)
    except OperationError as exc:
        return _operation_error("fork", exc)
    return _result(
        "fork",
        f"Forked session {ctx.session_id} to {session_id}.",
        data={"session_id": session_id, "source_session_id": ctx.session_id},
    )


async def _agent_command(ctx: Any, args: list[str]) -> dict[str, Any]:
    registry = getattr(ctx.engine, "agent_registry", None)
    definitions = registry.definitions() if registry is not None else ()
    active = str(getattr(ctx.engine.config, "agent_name", "XBotv2"))
    action = args[0].lower() if args else "status"
    if not args or (action in {"status", "list"} and len(args) == 1):
        visible = [definition for definition in definitions if not definition.hidden]
        lines = [f"Active Agent: {active}"]
        if action == "list":
            lines.extend(
                f"{definition.name}  {definition.mode}  {definition.description}"
                for definition in visible
            )
        return _result(
            "agent",
            "\n".join(lines),
            data={
                "active": active,
                "agent_name": active,
                "agents": [
                    {
                        "name": definition.name,
                        "mode": definition.mode,
                        "description": definition.description,
                        "hidden": definition.hidden,
                    }
                    for definition in visible
                ],
            },
        )

    target = args[1] if action == "use" and len(args) == 2 else None
    if target is None and len(args) == 1 and action not in {"status", "list", "use"}:
        target = args[0]
    if target is None:
        return _result(
            "agent",
            "Usage: /agent [list|status|use <name>|<name>]",
            status="error",
        )
    try:
        data = await select_agent(ctx, target)
    except OperationError as exc:
        return _operation_error("agent", exc)
    if data["active"] == active:
        return _result(
            "agent",
            f"Agent {active} is already active.",
            data=data,
        )
    return _result(
        "agent",
        f"Switched Agent from {active} to {data['active']}.",
        data=data,
    )


async def _tasks_command(ctx: Any, args: list[str]) -> dict[str, Any]:
    if args not in ([], ["ps"]):
        return _result("tasks", "Usage: /tasks [ps]", status="error")
    tasks = task_snapshots(ctx)
    if not tasks:
        return _result("tasks", "No background tasks.", data={"tasks": []})
    lines = [
        f"{task.get('kind', 'shell')}  {task['task_id']}  "
        f"{task['status']}  {task['command']}"
        for task in tasks
    ]
    return _result("tasks", "\n".join(lines), data={"tasks": tasks})


async def _task_command(ctx: Any, args: list[str]) -> dict[str, Any]:
    if len(args) == 2 and args[0] == "stop":
        try:
            data = await stop_task(ctx, args[1])
        except OperationError as exc:
            return _operation_error("task", exc)
        return _result(
            "task",
            f"Stopped background task {args[1]}.",
            data=data,
        )
    if args == ["stopall"]:
        stopped = await stop_all_tasks(ctx)
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
        provider_name = args[1]
        try:
            data = await select_provider(ctx, provider_name)
        except OperationError as exc:
            return _operation_error("provider", exc)
        return _result(
            "provider",
            f"Provider switched to {provider_name} ({data['model']}) for this session.",
            data=data,
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
        reload_live_policies(ctx)
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
        reload_live_policies(ctx)
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
        "agent_name": str(getattr(ctx.engine.config, "agent_name", "XBotv2")),
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


def _operation_error(command: str, exc: OperationError) -> dict[str, Any]:
    return _result(
        command,
        exc.message,
        status="error",
        data={"code": exc.code, "retryable": exc.retryable},
    )


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
            name="agent",
            slash="/agent [list|status|use <name>|<name>]",
            description="List or switch the active primary Agent.",
            handler=_agent_command,
            examples=[
                "/agent",
                "/agent list",
                "/agent use Explorer",
                "/agent default",
            ],
            parameters={"name": "Registered primary or all-mode Agent name."},
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
            description="List background shell and subagent tasks.",
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
