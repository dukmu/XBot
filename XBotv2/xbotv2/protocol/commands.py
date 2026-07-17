"""Plugin slash command discovery and execution for human clients."""

from __future__ import annotations

from typing import Any

from xbotv2.api.commands import Command
from xbotv2.core.session import SessionRuntime


def list_commands(*, extra: tuple[Command, ...] = ()) -> list[dict[str, Any]]:
    """Describe registered plugin commands and prompt expansions."""
    return [_command_dict(command) for command in extra]


async def execute_command(
    ctx: SessionRuntime,
    command: str,
    args: list[str],
    *,
    kind: str = "server",
    raw_args: str = "",
) -> dict[str, Any]:
    """Execute one plugin-owned server command.

    Prompt expansions are submitted through the message endpoint and built-in
    human commands are executed by clients through typed resource endpoints.
    """
    del args
    command = command.lower().strip().removeprefix("/")
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


def _command_dict(command: Command) -> dict[str, Any]:
    return {
        "name": command.name,
        "slash": f"/{command.name}",
        "kind": command.kind,
        "description": command.description,
        "usage": command.usage or f"/{command.name}",
        "examples": list(command.examples),
        "parameters": command.parameters,
    }


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
