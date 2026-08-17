"""Application use cases shared by human and machine interfaces."""

from __future__ import annotations

import secrets
import shutil
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from XBotv2.core.agents import AgentDefinition
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
    registry = ctx.services.agents.registry
    definition = registry.get(name) if registry is not None else None
    if definition is None or definition.mode == "subagent":
        raise OperationError(
            "agent_not_found",
            f"Unknown primary Agent: {name}",
        )
    active = ctx.engine.settings.agent_name
    if definition.name != active:
        async with ctx.turn_lock:
            await _activate_agent(ctx, definition)
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
        definition = ctx.services.agents.registry.get(active)
        if definition is None or definition.mode == "subagent":
            raise OperationError(
                "agent_not_found",
                f"Active Agent definition no longer exists: {active}",
            )
        await _activate_agent(ctx, definition)
    return {
        "active": active,
        "agents": ctx.services.agents.registry.definitions(),
    }


async def select_provider(ctx: Any, name: str) -> dict[str, str]:
    _require_idle(ctx, "switch provider")
    llm = ctx.services.llm
    store = ctx.services.state_store
    if name not in llm.names():
        raise OperationError(
            "provider_not_found",
            f"Unknown provider: {name}",
        )
    async with ctx.turn_lock:
        config = llm.provider_config(
            name, require_key=not ctx.engine.settings.llm_is_override
        )
        if not ctx.engine.settings.llm_is_override:
            ctx.services.model.replace(llm.create(
                config,
                media_root=str(store.root),
            ))
        ctx.engine.configure(
            model_client=ctx.services.model,
            provider=name,
            model=config.model,
            model_mode=config.model_mode,
            context_window=config.max_context_tokens,
            max_output_tokens=config.max_output_tokens,
        )
        ctx.provider_name = name
        store.provider = name
        ctx.engine.session.provider = name
        metadata = store.read_thread_metadata()
        metadata.update({
            "provider": name,
            "model": config.model,
            "model_mode": config.model_mode,
            "context_window": config.max_context_tokens,
        })
        store.write_thread_metadata(metadata)
    return {
        "provider": name,
        "model": config.model,
        "model_mode": config.model_mode,
    }


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


async def _activate_agent(
    ctx: Any, definition: AgentDefinition
) -> None:
    services = ctx.services
    config = services.settings.load_runtime_config(
        Path(ctx.workspace_root), ctx.session_id
    )
    services.agents.apply_definition(config, definition)
    provider_name = definition.provider or ctx.provider_name
    config.provider = provider_name
    provider = ctx.services.llm.provider_config(
        provider_name, require_key=not ctx.engine.settings.llm_is_override
    )
    services.agents.apply_provider(provider, definition)
    config.max_context_tokens = (
        definition.context_window or provider.max_context_tokens
    )
    config.max_output_tokens = provider.max_output_tokens
    if not ctx.engine.settings.llm_is_override:
        ctx.services.model.replace(ctx.services.llm.create(
            provider,
            media_root=str(ctx.services.state_store.root),
        ))

    _apply_live_policies(ctx, config)
    ctx.services.permissions.configure_agent(definition.permissions)
    ctx.engine.configure(
        model_client=ctx.services.model,
        max_iterations=definition.max_iterations or 200,
        provider=provider_name,
        model=provider.model,
        model_mode=provider.model_mode,
        context_window=config.max_context_tokens,
        max_output_tokens=config.max_output_tokens,
        agent_name=config.agent_name,
        agent_role=config.agent_role,
        developer_instructions=config.instructions,
        agent_instructions=config.agent_instructions,
        memory=config.memory,
    )
    services.agents.apply_tools(ctx.engine.tools.registry, config, definition)

    ctx.provider_name = provider_name
    store = ctx.services.state_store
    store.provider = provider_name
    ctx.engine.session.provider = provider_name
    metadata = store.read_thread_metadata()
    metadata.update({
        "agent": definition.name,
        "agent_definition": asdict(definition),
        "provider": provider_name,
        "model": provider.model,
        "model_mode": provider.model_mode,
        "context_window": config.max_context_tokens,
    })
    store.write_thread_metadata(metadata)


def reload_live_policies(ctx: Any) -> None:
    """Rebuild active permission and sandbox objects after config changes."""
    services = ctx.services
    base_config = services.settings.load_runtime_config(
        Path(ctx.workspace_root), ctx.session_id
    )
    metadata = ctx.services.state_store.read_thread_metadata()
    stored_definition = metadata.get("agent_definition")
    if isinstance(stored_definition, dict):
        definition = AgentDefinition(**{
            key: tuple(value) if key in {"tools", "disabled_tools"}
            and isinstance(value, list) else value
            for key, value in stored_definition.items()
        })
        services.agents.apply_definition(
            base_config,
            definition,
        )
    _apply_live_policies(ctx, base_config)
    if isinstance(stored_definition, dict):
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
