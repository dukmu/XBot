"""Human commands owned by the session component.

``/status``, ``/clear``, ``/undo``, and ``/fork`` are registered by the
session component itself; handlers reuse the application use cases.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from XBotv2.core.commands import (
    Command,
    CommandResult,
    command_usage,
    guard_command,
    run_command_operation,
    split_command_args,
)
from XBotv2.core.errors import OperationError
from XBotv2.core.events import EventContext, Events
from XBotv2.core.history import display_history
from XBotv2.session.session import fork_session


async def status_command(ctx: Any, raw_args: str) -> CommandResult:
    if raw_args.strip():
        return command_usage("/status")
    settings = ctx.engine.settings
    data = {
        "session_id": ctx.session_id,
        "thread_id": ctx.thread_id,
        "provider": settings.provider,
        "model": settings.model,
        "model_mode": settings.model_mode,
        "context_window": ctx.engine.context_window,
        "agent_name": settings.agent_name,
        "workspace_root": ctx.workspace_root,
    }
    return CommandResult(
        " ".join(
            f"{key}={data[key]}"
            for key in ("session_id", "thread_id", "provider", "model")
        ),
        data=data,
    )


async def clear_command(ctx: Any, raw_args: str) -> CommandResult:
    if raw_args.strip():
        return command_usage("/clear")
    return await run_command_operation(
        ctx.services.session.clear_history(),
        lambda removed: CommandResult(
            f"Cleared {removed} conversation turns.",
            data={"removed_turns": removed, "messages": []},
            history=[],
        ),
    )


async def undo_command(ctx: Any, raw_args: str) -> CommandResult:
    parts = split_command_args(raw_args)
    if len(parts) > 1:
        return command_usage("/undo [count]")
    try:
        count = int(parts[0]) if parts else 1
    except ValueError:
        return CommandResult(
            "Undo count must be a positive integer.",
            status="error",
            data={"code": "invalid_count"},
        )
    if count < 1:
        return CommandResult(
            "Undo count must be a positive integer.",
            status="error",
            data={"code": "invalid_count"},
        )
    return await run_command_operation(
        ctx.services.session.undo_history(count),
        lambda messages: CommandResult(
            f"Removed {count} conversation turn(s).",
            data={"removed_turns": count, "messages": display_history(messages)},
            history=display_history(messages),
        ),
    )


async def fork_command(ctx: Any, raw_args: str) -> CommandResult:
    if raw_args.strip():
        return command_usage("/fork")
    return await run_command_operation(
        fork_session(ctx.paths, ctx.session_id, [ctx]),
        lambda session_id: CommandResult(
            f"Forked session to {session_id}.",
            data={"session_id": session_id},
        ),
    )


async def reload_session(ctx: Any) -> dict[str, Any]:
    """System soft restart: validate then fan out the restart event.

    The LLM service validates its merged provider catalog first (fail-closed
    before any entry is re-applied), then ``SOFT_RELOAD`` is emitted: the
    loader re-applies the external tree layer, the agents service rebinds the
    active model, and workspace_instructions re-reads its workspace sources.
    """
    llm = ctx.services.get("llm")
    if llm is None:
        raise OperationError("plugin_unavailable", "LLM plugin is not loaded.")
    merged = llm.validate_catalog(ctx.paths, Path(ctx.workspace_root))
    providers = merged.get("providers") or {}
    default = str(merged.get("default") or llm.default_name())
    if ctx.turn_lock.locked():
        raise OperationError(
            "thread_busy",
            "Cannot reload config while a turn is active.",
            retryable=True,
        )
    result: dict[str, Any] = {}
    async with ctx.turn_lock:
        await ctx.services.emit(Events.SOFT_RELOAD, EventContext(event={
            "scope": "system",
            "config_path": str(ctx.paths.config_dir / "plugins.yaml"),
            "values": _reload_values(ctx),
            "result": result,
        }))
    reloaded = list(result.get("reloaded") or [])
    loader = ctx.services.get("loader")
    if loader is not None and "workspace_instructions" in loader.loaded_ids:
        reloaded.append("workspace_instructions")
    return {
        "reloaded": reloaded,
        "errors": list(result.get("errors") or []),
        "provider": result.get("provider") or ctx.engine.settings.provider,
        "model": result.get("model") or ctx.engine.settings.model,
        "model_mode": result.get("model_mode") or ctx.engine.settings.model_mode,
        "context_window": result.get("context_window") or ctx.engine.context_window,
        "default": default,
        "providers": providers,
    }


async def reload_command(ctx: Any, raw_args: str) -> CommandResult:
    if raw_args.strip():
        return command_usage("/reload")
    return await run_command_operation(
        reload_session(ctx),
        lambda data: (
            f"Reloaded {', '.join(data['reloaded'])}: "
            f"{data['provider']} ({data['model']})"
            + ("; errors: " + "; ".join(data["errors"]) if data.get("errors") else "")
        ),
    )


def _reload_values(ctx: Any) -> dict[str, Any]:
    """Reconstruct the tree interpolation values used at session boot."""
    session = ctx.services.get("session", strict=False)
    disabled = frozenset(
        entry.id
        for entry in ctx.services.get("loader").tree.entries
        if entry.disabled
    )
    return {
        "paths": ctx.paths,
        "session_paths": session.session_paths if session is not None else None,
        "session_id": ctx.session_id,
        "thread_id": ctx.thread_id,
        "workspace_root": Path(ctx.workspace_root),
        "provider_name": ctx.provider_name,
        "parent_permission_system": ctx.services.get("permissions", strict=False),
        "interactive": ctx.interactive,
        "disabled": disabled,
    }


SESSION_COMMANDS: tuple[Command, ...] = (
    Command(
        name="status",
        description="Show the current session and thread status",
        handler=guard_command(status_command),
        usage="/status",
    ),
    Command(
        name="clear",
        description="Clear conversation history",
        handler=guard_command(clear_command),
        usage="/clear",
    ),
    Command(
        name="undo",
        description="Remove recent conversation turns",
        handler=guard_command(undo_command),
        usage="/undo [count]",
    ),
    Command(
        name="fork",
        description="Fork the persisted session",
        handler=guard_command(fork_command),
        usage="/fork",
    ),
    Command(
        name="reload",
        description="System soft restart: re-read config overlays and re-apply plugins",
        handler=guard_command(reload_command),
        usage="/reload",
    ),
)


__all__ = [
    "SESSION_COMMANDS",
    "status_command",
    "clear_command",
    "undo_command",
    "fork_command",
    "reload_session",
    "reload_command",
]
