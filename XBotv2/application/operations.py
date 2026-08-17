"""Application use cases shared by human and machine interfaces."""

from __future__ import annotations

import secrets
import shutil
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Any

from XBotv2.core.events import EventContext, Events
from XBotv2.core.paths import RuntimePaths


class OperationError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


async def clear_history(ctx: Any) -> int:
    _require_idle(ctx, "rewrite history")
    async with ctx.turn_lock:
        removed_turns = sum(
            message.role == "user" for message in ctx.engine.messages
        )
        await _replace_history(ctx, [], operation="clear")
    return removed_turns


async def undo_history(ctx: Any, count: int) -> list[Any]:
    _require_idle(ctx, "rewrite history")
    if count < 1:
        raise OperationError(
            "invalid_undo_count",
            "Undo count must be a positive integer.",
        )
    async with ctx.turn_lock:
        messages = list(ctx.engine.messages)
        user_indexes = [
            index for index, message in enumerate(messages)
            if message.role == "user"
        ]
        if count > len(user_indexes):
            raise OperationError(
                "invalid_undo_count",
                f"Cannot undo {count} turns; session has {len(user_indexes)}.",
            )
        kept = messages[:user_indexes[-count]]
        await _replace_history(ctx, kept, operation="undo", turns=count)
    return kept


async def _replace_history(
    ctx: Any,
    messages: list[Any],
    *,
    operation: str,
    turns: int = 0,
) -> None:
    state = ctx.engine.state
    state.replace_messages(messages)
    await ctx.services.emit(Events.STATE_CHANGED, EventContext(
        messages=state.messages,
        session=state.session,
        event={"history_operation": (operation, turns)},
    ))


def fork_persisted_session(paths: RuntimePaths, source_session_id: str) -> str:
    session_id = _new_fork_id()
    while paths.session(session_id).root.exists():
        session_id = _new_fork_id()
    source = paths.session(source_session_id).root
    target = paths.session(session_id).root
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return session_id


async def fork_session(
    paths: RuntimePaths,
    source_session_id: str,
    *contexts: Any,
) -> str:
    """Persist and copy one idle session while all live threads are locked."""
    require_forkable(*contexts)
    async with AsyncExitStack() as stack:
        for ctx in sorted(contexts, key=lambda item: item.thread_id):
            await stack.enter_async_context(ctx.turn_lock)
        for ctx in contexts:
            await ctx.services.persistence.flush()
        return fork_persisted_session(paths, source_session_id)


def require_forkable(*contexts: Any) -> None:
    for ctx in contexts:
        _require_idle(ctx, "fork")
        if any(
            task.get("status") in {"pending", "running"}
            for task in task_snapshots(ctx)
        ):
            raise OperationError(
                "thread_busy",
                "Cannot fork while a background task is active.",
                retryable=True,
            )


async def select_agent(ctx: Any, name: str) -> dict[str, Any]:
    _require_idle(ctx, "switch Agent")
    definition = ctx.services.agents.definition(name)
    if definition is None or definition.mode == "subagent":
        raise OperationError(
            "agent_not_found",
            f"Unknown primary Agent: {name}",
        )
    active = ctx.engine.settings.agent_name
    if definition.name != active:
        async with ctx.turn_lock:
            selected = await ctx.services.agents.activate(definition.name)
            ctx.provider_name = selected["provider"]
    return {
        "active": definition.name,
        "agent_name": definition.name,
        "provider": ctx.provider_name,
        "model": ctx.engine.settings.model,
        "model_mode": ctx.engine.settings.model_mode,
        "context_window": ctx.engine.context_window,
    }


async def reload_agents(ctx: Any) -> dict[str, Any]:
    """Reload Agent definitions and reapply the active primary Agent."""
    _require_idle(ctx, "reload Agents")
    loader = ctx.services.get("loader")
    if loader is None:
        raise OperationError("plugin_unavailable", "Agent plugin is not loaded.")
    active = ctx.engine.settings.agent_name
    async with ctx.turn_lock:
        if not await loader.reload("agents"):
            raise OperationError("plugin_unavailable", "Agent plugin is not loaded.")
        definition = ctx.services.agents.definition(active)
        if definition is None or definition.mode == "subagent":
            raise OperationError(
                "agent_not_found",
                f"Active Agent definition no longer exists: {active}",
            )
        selected = await ctx.services.agents.activate(definition.name)
        ctx.provider_name = selected["provider"]
    return {
        "active": active,
        "agents": ctx.services.agents.definitions(),
    }


async def select_provider(ctx: Any, name: str) -> dict[str, str]:
    _require_idle(ctx, "switch provider")
    async with ctx.turn_lock:
        try:
            selected = await ctx.services.agents.select_provider(name)
        except ValueError as error:
            raise OperationError(
                "provider_not_found",
                str(error),
            ) from error
        ctx.provider_name = selected["provider"]
    return selected


def task_snapshots(ctx: Any) -> list[dict[str, Any]]:
    registry = ctx.services.get("jobs")
    if registry is None:
        return []
    return [registry.snapshot(job) for job in registry.all()]


async def stop_task(ctx: Any, task_id: str) -> dict[str, Any]:
    registry = ctx.services.get("jobs")
    if registry is None:
        raise OperationError("task_not_found", f"Unknown task: {task_id}")
    job = registry.get_or_none(task_id)
    if job is None:
        raise OperationError("task_not_found", f"Unknown task: {task_id}")
    await registry.cancel(task_id)
    return registry.snapshot(job)


async def stop_all_tasks(ctx: Any) -> list[dict[str, Any]]:
    registry = ctx.services.get("jobs")
    if registry is None:
        return []
    return await registry.stop_all()


async def update_session_policy(
    *,
    paths: RuntimePaths,
    session_id: str,
    contexts: list[Any],
    permissions: dict[str, str] | None = None,
    remove_permissions: list[str] | None = None,
    sandbox: dict[str, Any] | None = None,
    remove_sandbox: list[str] | None = None,
) -> dict[str, Any]:
    """Persist a session policy patch and apply it to every live thread."""
    for ctx in contexts:
        _require_idle(ctx, "update session policy")
        if any(
            task.get("status") in {"pending", "running"}
            for task in task_snapshots(ctx)
        ):
            raise OperationError(
                "thread_busy",
                "Cannot update session policy while a background task is active.",
                retryable=True,
            )
    async with AsyncExitStack() as stack:
        for ctx in sorted(contexts, key=lambda item: item.thread_id):
            await stack.enter_async_context(ctx.turn_lock)
        if not contexts:
            raise OperationError(
                "thread_not_active",
                "Session policy updates require one active thread.",
            )
        policy = contexts[0].services.settings.patch_session_policy(
            session_id=session_id,
            permissions=permissions,
            remove_permissions=remove_permissions or (),
            sandbox=sandbox,
            remove_sandbox=remove_sandbox or (),
        )
        for ctx in contexts:
            reload_live_policies(ctx)
    return policy


def reload_live_policies(ctx: Any) -> None:
    """Rebuild active permission and sandbox objects after config changes."""
    services = ctx.services
    definition = services.agents.active_definition()
    base_config = services.agents.runtime_config(definition)
    _apply_live_policies(ctx, base_config)
    if definition is not None:
        ctx.services.permissions.configure_agent(definition.permissions)


def _apply_live_policies(ctx: Any, config: Any) -> None:
    """Apply one already-resolved policy to live runtime objects."""
    permissions = config.permissions
    sandbox = config.sandbox
    ctx.services.permissions.replace_rules(permissions)
    ctx.services.sandbox.replace_config(sandbox)


def _require_idle(ctx: Any, action: str) -> None:
    if ctx.turn_lock.locked():
        raise OperationError(
            "thread_busy",
            f"Cannot {action} while a turn is active.",
            retryable=True,
        )


def _new_fork_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


__all__ = [
    "OperationError",
    "clear_history",
    "fork_persisted_session",
    "fork_session",
    "reload_agents",
    "reload_live_policies",
    "require_forkable",
    "select_agent",
    "select_provider",
    "stop_all_tasks",
    "stop_task",
    "task_snapshots",
    "undo_history",
    "update_session_policy",
]
