"""Runtime state mutations shared by human and machine interfaces."""

from __future__ import annotations

import secrets
import shutil
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from xbotv2.api.agents import AgentDefinition
from xbotv2.api.paths import RuntimePaths
from xbotv2.core.session import SessionRuntime


class OperationError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


async def clear_history(ctx: SessionRuntime) -> int:
    _require_idle(ctx, "rewrite history")
    async with ctx.turn_lock:
        removed_turns = sum(
            message.role == "user" for message in ctx.engine.messages
        )
        await ctx.engine.replace_history([], operation="clear")
    return removed_turns


async def undo_history(ctx: SessionRuntime, count: int) -> list[Any]:
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
        await ctx.engine.replace_history(kept, operation="undo", turns=count)
    return kept


async def fork_session(ctx: SessionRuntime) -> str:
    require_forkable(ctx)
    async with ctx.turn_lock:
        await ctx.engine.save_messages()
        return fork_persisted_session(ctx.paths, ctx.session_id)


def fork_persisted_session(paths: RuntimePaths, source_session_id: str) -> str:
    session_id = _new_fork_id()
    while paths.session(session_id).root.exists():
        session_id = _new_fork_id()
    source = paths.session(source_session_id).root
    target = paths.session(session_id).root
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return session_id


def require_forkable(*contexts: SessionRuntime) -> None:
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


async def select_agent(ctx: SessionRuntime, name: str) -> dict[str, Any]:
    _require_idle(ctx, "switch Agent")
    registry = getattr(ctx.engine, "agent_registry", None)
    definition = registry.get(name) if registry is not None else None
    if definition is None or definition.mode == "subagent":
        raise OperationError(
            "agent_not_found",
            f"Unknown primary Agent: {name}",
        )
    active = str(getattr(ctx.engine.config, "agent_name", "XBotv2"))
    if definition.name != active:
        async with ctx.turn_lock:
            await _activate_agent(ctx, definition)
    return {
        "active": definition.name,
        "agent_name": definition.name,
        "provider": ctx.provider_name,
        "model": str(getattr(ctx.engine, "model", "")),
        "context_window": int(getattr(ctx.engine, "context_window", 0)),
    }


async def select_provider(ctx: SessionRuntime, name: str) -> dict[str, str]:
    _require_idle(ctx, "switch provider")
    from xbotv2.config.loader import load_provider_config, load_provider_names
    from xbotv2.llm.client import create_llm

    _default, names = load_provider_names(ctx.paths)
    if name not in names:
        raise OperationError(
            "provider_not_found",
            f"Unknown provider: {name}",
        )
    async with ctx.turn_lock:
        config = load_provider_config(ctx.paths, name)
        if not getattr(ctx.engine, "llm_is_override", False):
            ctx.engine.llm = create_llm(config)
        ctx.engine.model = config.model
        ctx.provider_name = name
        ctx.engine.config.provider = name
        ctx.engine.state_store.provider = name
        if ctx.engine.session is not None:
            ctx.engine.session.provider = name
        metadata = ctx.engine.state_store.read_thread_metadata()
        metadata.update({"provider": name, "model": config.model})
        ctx.engine.state_store.write_thread_metadata(metadata)
    return {"provider": name, "model": config.model}


def task_snapshots(ctx: SessionRuntime) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    background = getattr(ctx.engine, "background_tasks", None)
    subagents = getattr(ctx.engine, "subagents", None)
    if background is not None:
        tasks.extend(background.snapshots())
    if subagents is not None:
        tasks.extend(subagents.snapshots())
    return tasks


async def stop_task(ctx: SessionRuntime, task_id: str) -> dict[str, Any]:
    background = getattr(ctx.engine, "background_tasks", None)
    subagents = getattr(ctx.engine, "subagents", None)
    manager = subagents if task_id.startswith("agent-task-") else background
    if manager is None:
        raise OperationError("task_not_found", f"Unknown task: {task_id}")
    result = await manager.stop_task(task_id)
    if result.status != "success":
        code = (
            result.error.code if result.error is not None else "task_stop_failed"
        )
        retryable = bool(result.error and result.error.retryable)
        raise OperationError(code, str(result.content), retryable=retryable)
    return dict(result.data) if isinstance(result.data, dict) else {}


async def stop_all_tasks(ctx: SessionRuntime) -> list[dict[str, Any]]:
    stopped: list[dict[str, Any]] = []
    background = getattr(ctx.engine, "background_tasks", None)
    subagents = getattr(ctx.engine, "subagents", None)
    if background is not None:
        stopped.extend(await background.stop_all())
    if subagents is not None:
        stopped.extend(await subagents.stop_all())
    return stopped


async def update_session_policy(
    *,
    paths: RuntimePaths,
    session_id: str,
    contexts: list[SessionRuntime],
    permissions: dict[str, str] | None = None,
    remove_permissions: list[str] | None = None,
    sandbox: dict[str, Any] | None = None,
    remove_sandbox: list[str] | None = None,
) -> dict[str, Any]:
    """Persist a session policy patch and apply it to every live thread."""
    from xbotv2.config.policy import patch_session_policy

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
        policy = patch_session_policy(
            paths=paths,
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
    ctx: SessionRuntime, definition: AgentDefinition
) -> None:
    from xbotv2.config.loader import load_provider_config, load_system_config
    from xbotv2.core.agents import (
        apply_agent_definition,
        apply_agent_provider,
        apply_agent_tools,
    )
    from xbotv2.llm.client import create_llm

    config = load_system_config(ctx.paths, Path(ctx.workspace_root))
    apply_agent_definition(config, definition)
    provider_name = definition.provider or ctx.provider_name
    config.provider = provider_name
    provider = load_provider_config(ctx.paths, provider_name)
    apply_agent_provider(provider, definition)
    llm = (
        ctx.engine.llm
        if getattr(ctx.engine, "llm_is_override", False)
        else create_llm(provider)
    )

    ctx.engine.config = config
    ctx.engine.startup_config = config.model_copy(deep=True)
    reload_live_policies(ctx)
    ctx.engine.llm = llm
    ctx.engine.model = provider.model
    ctx.engine.context_window = config.max_context_tokens
    ctx.engine.max_iterations = definition.max_iterations or 50
    apply_agent_tools(ctx.engine.tool_registry, config, definition)

    ctx.provider_name = provider_name
    ctx.engine.state_store.provider = provider_name
    if ctx.engine.session is not None:
        ctx.engine.session.provider = provider_name
    metadata = ctx.engine.state_store.read_thread_metadata()
    metadata.update({
        "agent": definition.name,
        "agent_definition": asdict(definition),
        "provider": provider_name,
        "model": provider.model,
        "context_window": config.max_context_tokens,
    })
    ctx.engine.state_store.write_thread_metadata(metadata)


def reload_live_policies(ctx: SessionRuntime) -> None:
    """Rebuild active permission and sandbox objects after config changes."""
    from xbotv2.config.loader import load_system_config
    from xbotv2.config.policy import (
        load_session_policy,
        merge_permission_config,
        merge_sandbox_config,
    )
    from xbotv2.tools.permissions import PermissionIntersection, PermissionSystem
    from xbotv2.tools.sandbox import SandboxPolicy

    base_config = getattr(ctx.engine, "startup_config", None)
    if base_config is None:
        base_config = load_system_config(ctx.paths, Path(ctx.workspace_root))
    session_policy = load_session_policy(ctx.paths, ctx.session_id)
    permissions = merge_permission_config(
        base_config.permissions,
        session_policy.get("permissions"),
    )
    sandbox = merge_sandbox_config(
        base_config.sandbox,
        session_policy.get("sandbox"),
    )
    ctx.engine.config.permissions = permissions
    ctx.engine.config.sandbox = sandbox
    live_permissions = ctx.engine.permission_system
    if isinstance(live_permissions, PermissionIntersection):
        live_permissions.child.replace_rules(permissions)
    elif isinstance(live_permissions, PermissionSystem):
        live_permissions.replace_rules(permissions)
    else:
        ctx.engine.permission_system = PermissionSystem(permissions)
    live_sandbox = ctx.engine.sandbox_policy
    if isinstance(live_sandbox, SandboxPolicy):
        live_sandbox.replace_config(sandbox)
    else:
        ctx.engine.sandbox_policy = SandboxPolicy(
            sandbox,
            data_root=ctx.paths.data_dir,
            workspace_root=Path(ctx.workspace_root),
            session_root=getattr(live_sandbox, "session_root", None),
        )


def _require_idle(ctx: SessionRuntime, action: str) -> None:
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
