"""Application use cases shared by human and machine interfaces."""

from __future__ import annotations

import secrets
import shutil
from pathlib import Path

import yaml
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Any

from XBotv2.config.tree import DEFAULT_TREE
from XBotv2.core.events import EventContext, Events
from XBotv2.core.paths import RuntimePaths
from XBotv2.llm.config import parse_provider_config
from XBotv2.loader import PluginTree


# Entries whose live re-apply would destroy session-lifecycle state the
# engine or the session runtime holds (loop state, message stores, running
# jobs, the tool registry, the Agent service itself).  Config changes for
# these entries require a process restart; ``workspace_instructions`` is
# handled separately because its apply re-applies the workspace overlay.
_RELOAD_PROTECTED = frozenset({
    "session",
    "persistence",
    "jobs",
    "agentloop",
    "agents-service",
    "workspace_instructions",
})


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
            persistence = ctx.services.get("persistence", strict=False)
            if persistence is None:
                raise OperationError(
                    "persistence_unavailable",
                    "Cannot fork a live session while message persistence "
                    "is disabled",
                )
            await persistence.flush()
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
        # Workspace Agent definitions are discovered by the workspace
        # plugin; reload it so workspace edits apply with the same command.
        await loader.reload("workspace_instructions")
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


async def select_provider(
    ctx: Any,
    name: str,
    model: str | None = None,
) -> dict[str, str]:
    _require_idle(ctx, "switch provider")
    async with ctx.turn_lock:
        try:
            selected = await ctx.services.agents.select_provider(name, model=model)
        except ValueError as error:
            code = (
                "model_not_found"
                if "Unknown model" in str(error)
                else "provider_not_found"
            )
            raise OperationError(code, str(error)) from error
        ctx.provider_name = selected["provider"]
    return selected


async def reload_config(ctx: Any) -> dict[str, Any]:
    """Soft-restart the session: re-read config overlays and re-apply plugins.

    Re-reads the external plugin tree layers (global ``config/plugins.yaml``
    + workspace ``.xbot/plugins.yaml``), validates the merged LLM provider
    catalog, then hot-applies every changed entry through the loader
    (reload changed entries, mount new ones, unload disabled ones) and
    re-binds the active model client.  Invalid provider sections fail the
    whole reload before anything is touched; a failing entry or a missing
    active binding keeps the previous client and is reported, matching dsh
    settings last-good semantics.  Entries the live session cannot safely
    re-apply (session/persistence/jobs/agentloop) are reported as
    restart-required instead of being reloaded.
    """
    _require_idle(ctx, "reload config")
    loader = ctx.services.get("loader")
    if loader is None:
        raise OperationError("plugin_unavailable", "Loader plugin is not loaded.")
    merged = _merged_llm_config(ctx.paths, Path(ctx.workspace_root))
    providers = merged.get("providers") or {}
    llm = ctx.services.get("llm")
    default = str(merged.get("default") or llm.default_name())

    errors: list[str] = []
    for name, raw in providers.items():
        try:
            parse_provider_config(dict(raw), require_key=False)
        except Exception as error:  # noqa: BLE001 - report catalog errors
            errors.append(f"{name}: {error}")
    if errors:
        raise OperationError(
            "config_invalid",
            "Provider catalog is invalid: " + "; ".join(errors),
        )

    values = _reload_values(ctx)
    # The workspace overlay is re-applied by the workspace_instructions
    # plugin below (its apply re-reads ``.xbot/plugins.yaml`` and patches the
    # tree with ``_patch_owner`` set so it never reloads itself); only the
    # global layer is patched here, so every entry is re-applied once.
    patch = _config_layer(ctx.paths.config_dir / "plugins.yaml", values)

    reloaded: list[str] = []
    notices: list[str] = []
    async with ctx.turn_lock:
        for entry in patch.entries:
            if entry.id in _RELOAD_PROTECTED:
                notices.append(
                    f"{entry.id}: cannot be hot-reloaded; restart required"
                )
                continue
            try:
                affected = await loader.apply_patch(PluginTree([entry]))
                if not entry.disabled and entry.id not in loader.loaded_ids:
                    notices.append(f"{entry.id}: reload failed; keeping last good")
                reloaded.extend(affected)
            except Exception as error:  # noqa: BLE001 - keep last good per entry
                notices.append(f"{entry.id}: {error}")

        if "workspace_instructions" in loader.loaded_ids:
            if await loader.reload("workspace_instructions"):
                reloaded.append("workspace_instructions")
            else:
                notices.append("workspace_instructions: reload failed")

        active = ctx.engine.settings.agent_name
        definition = ctx.services.agents.definition(active)
        if definition is not None and definition.mode != "subagent":
            try:
                selected = await ctx.services.agents.activate(active)
            except Exception as error:  # noqa: BLE001 - keep last good binding
                notices.append(f"active agent {active}: {error}")
                selected = await ctx.services.agents.apply_provider_catalog(
                    default, providers
                )
        else:
            selected = await ctx.services.agents.apply_provider_catalog(
                default, providers
            )
        ctx.provider_name = selected["provider"]
    return {
        **selected,
        "reloaded": reloaded,
        "errors": notices,
        "default": default,
        "providers": providers,
    }


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


def _config_layer(path: Path, values: dict[str, Any]) -> PluginTree:
    """Read one external plugin-tree layer; a missing file is an empty layer."""
    if not path.is_file():
        return PluginTree([])
    return PluginTree.from_yaml(path, values=values)


def _merged_llm_config(paths: Any, workspace_root: Path) -> dict[str, Any]:
    """Re-read the merged ``llm`` entry config from all configuration layers."""

    def llm_config(document: list[dict[str, Any]]) -> dict[str, Any]:
        for entry in document:
            if entry.get("id") == "llm":
                return dict(entry.get("config") or {})
        return {}

    merged: dict[str, Any] = {}
    for source in (
        DEFAULT_TREE,
        paths.config_dir / "plugins.yaml",
        workspace_root / ".xbot" / "plugins.yaml",
    ):
        path = Path(source)
        if not path.is_file():
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        merged = _merge_config(merged, llm_config(document))
    return merged


def _merge_config(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        merged[key] = (
            _merge_config(current, value)
            if isinstance(current, dict) and isinstance(value, dict)
            else value
        )
    return merged


async def select_effort(ctx: Any, effort: str) -> dict[str, Any]:
    """Switch the active model's reasoning effort tier."""
    _require_idle(ctx, "switch effort")
    async with ctx.turn_lock:
        try:
            selected = await ctx.services.agents.select_effort(effort)
        except ValueError as error:
            raise OperationError("unsupported_effort", str(error)) from error
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
    "reload_config",
    "reload_live_policies",
    "require_forkable",
    "select_agent",
    "select_effort",
    "select_provider",
    "stop_all_tasks",
    "stop_task",
    "task_snapshots",
    "undo_history",
    "update_session_policy",
]
