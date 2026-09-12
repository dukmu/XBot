"""Start one XBot application and own its complete composition lifecycle.

This module is the application boundary corresponding to DSH's app boot: it
creates the root context, publishes launcher-owned entry services, mounts and
starts the configured plugin tree, then announces the initialized application.
It disposes partial state when startup fails; it does not construct an Engine
or implement Agent policy.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from pydantic import JsonValue

from xcore import Context

from XBotv2.application.boot import boot_application
from XBotv2.persistence.store import ThreadPersistence
from XBotv2.core.metadata import ThreadMetadataState
from XBotv2.core.filesystem.artifacts import ArtifactStore
from XBotv2.core.filesystem.session_lock import (
    SessionOwnership,
    acquire_session,
)
from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG
from XBotv2.application.child import ChildApplications
from XBotv2.application.client_events import ClientEventRouter
from XBotv2.application.host import mounted_application
from XBotv2.application.contracts import (
    AgentApplicationPort,
    ClientEventsPort,
    ParentPermissions,
    SessionLaunch,
)
from XBotv2.config.seed import ensure_initial_config
from XBotv2.application.tree import load_agent_tree
from XBotv2.agents import AgentCreateOptions, AgentDefinition
from XBotv2.session.contracts import AgentApplicationOptions
from XBotv2.session.contracts import new_session_id
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.providers import BaseProvider
from XBotv2.permissions import PermissionsPort

_IDENTIFIER_RE = __import__("re").compile(r"^[A-Za-z0-9._-]+$")


async def start_application(
    *,
    paths: RuntimePaths,
    provider_name: str = "default",
    session_id: str | None = None,
    thread_id: str = "agent",
    workspace_root: Path | str | None = None,
    no_plugins: bool = False,
    plugin_dirs: list[Path | str] | None = None,
    llm_override: BaseProvider | None = None,
    selected_agent: str | None = None,
    agent_definition: AgentDefinition | None = None,
    parent_permission_system: PermissionsPort | None = None,
    parent_thread_id: str = "",
    is_subagent: bool = False,
    interactive: bool = True,
    extra_plugins: list[dict[str, JsonValue]] | None = None,
    client_events: ClientEventsPort | None = None,
) -> Context:
    """Assemble the XBot runtime on an XCore context.

    Returns the owning XCore application context. Consumers obtain the loop
    driver from ``ctx.engine``; lifecycle and plugin services remain on the
    application instead of leaking through Engine. ``plugin_dirs`` only adds
    external import roots; ``no_plugins`` selects the core Agent composition.
    ``extra_plugins`` contains session-scoped configuration patches."""
    _validate_identifier("provider_name", provider_name)
    session_id = session_id or new_session_id()
    _validate_identifier("session_id", session_id)
    _validate_identifier("thread_id", thread_id)
    workspace_root = Path(workspace_root or Path.cwd()).resolve()

    # Subagent children share the parent's profile; only the root seeds it.
    if not is_subagent:
        ensure_initial_config(paths)

    session_paths = paths.session(session_id)
    session_preexisting = session_paths.root.exists()
    thread_preexisting = session_paths.has_thread(thread_id)
    thread_paths = session_paths.thread(thread_id)
    plugin_ctx: Context | None = None

    tree = load_agent_tree(
        paths=paths,
        workspace_root=workspace_root,
        is_subagent=is_subagent,
        no_plugins=no_plugins,
        plugin_dirs=plugin_dirs,
        extra_plugins=extra_plugins,
        session_id=session_id,
    )
    persistence_enabled = any(
        entry.id == "persistence" and not entry.disabled for entry in tree.entries
    )
    thread_persistence = (
        ThreadPersistence.create(
            thread_paths,
            thread_id=thread_id,
            workspace_root=str(workspace_root),
            provider=provider_name,
        )
        if persistence_enabled
        else None
    )
    artifacts = (
        thread_persistence.artifacts
        if thread_persistence is not None
        else ArtifactStore(
            thread_paths,
            DEFAULT_RUNTIME_LOG.bind(
                "persistence",
                session_id=session_id,
                thread_id=thread_id,
            ),
        )
    )

    children = ChildApplications(
        paths=paths,
        provider_name=provider_name,
        session_id=session_id,
        workspace_root=workspace_root,
        no_plugins=no_plugins,
        plugin_dirs=plugin_dirs,
        llm_override=llm_override,
        parent_thread_id=thread_id,
        interactive=interactive,
    )

    agent_options = AgentCreateOptions(
        session_id=session_id,
        thread_id=thread_id,
        workspace_root=str(workspace_root),
        provider_name=provider_name,
        agent_definition=agent_definition,
        model_override=llm_override,
        selected_agent=selected_agent,
        parent_thread_id=parent_thread_id,
        is_subagent=is_subagent,
    )

    services = {
        "runtime_paths": paths,
        "agent_options": agent_options,
        "session_launch": SessionLaunch(
            session_id=session_id,
            thread_id=thread_id,
            workspace_root=workspace_root,
            provider_name=provider_name,
            session_paths=session_paths,
            interactive=interactive,
            is_subagent=is_subagent,
        ),
        "parent_permissions": ParentPermissions(parent_permission_system),
        "plugin_overrides": extra_plugins or [],
        "plugin_dirs": plugin_dirs or [],
        "no_plugins": no_plugins,
        "client_events": ClientEventRouter(parent=client_events),
        "child_applications": children,
        "artifacts": artifacts,
        (
            "thread_persistence"
            if thread_persistence is not None
            else "thread_metadata"
        ): (
            thread_persistence
            if thread_persistence is not None
            else ThreadMetadataState()
        ),
    }

    ownership: SessionOwnership | None = None
    try:
        # One runtime owns a session: a second one must fail here instead of
        # writing turns into the same trajectory.
        ownership = acquire_session(
            session_paths.root,
            label=f"{session_id}/{thread_id}",
        )
        plugin_ctx = Context(
            data_dir=thread_paths.plugin_state_dir,
            state_service=(
                thread_persistence.state
                if thread_persistence is not None
                else None
            ),
        )
        plugin_ctx.dispose(ownership.release)
        for name, service in services.items():
            plugin_ctx.set(name, service)
        plugin_ctx = await boot_application(
            ctx=plugin_ctx,
            tree=tree,
            plugin_dirs=plugin_dirs,
        )
        await plugin_ctx.agent_runtime.announce_initialized()

        return plugin_ctx
    except BaseException as startup_error:
        if ownership is not None:
            ownership.release()
        if plugin_ctx is not None:
            try:
                await plugin_ctx.destroy()
            except BaseException as cleanup_error:
                startup_error.add_note(
                    "Application cleanup after Agent construction failed: "
                    f"{cleanup_error!r}"
                )
        if not thread_preexisting:
            if not session_preexisting:
                shutil.rmtree(session_paths.root, ignore_errors=True)
            else:
                shutil.rmtree(thread_paths.root, ignore_errors=True)
        raise


async def create_agent_application(
    options: AgentApplicationOptions,
) -> AgentApplicationPort:
    """Typed factory exported to composition roots, not session internals."""
    extra_plugins = (
        [
            {"id": name, "config": config}
            for name, config in options.plugin_configs.items()
        ]
        if options.plugin_configs
        else None
    )
    context = await start_application(
        paths=options.paths,
        provider_name=options.provider_name,
        session_id=options.session_id,
        thread_id=options.thread_id,
        workspace_root=options.workspace_root,
        no_plugins=options.no_plugins,
        extra_plugins=extra_plugins,
        llm_override=options.model_override,
        selected_agent=options.selected_agent,
        agent_definition=options.agent_definition,
        parent_thread_id=options.parent_thread_id,
        parent_permission_system=options.parent_permission_system,
        is_subagent=options.is_subagent,
        interactive=options.interactive,
    )
    return mounted_application(context)


def _validate_identifier(field: str, value: str) -> None:
    if not value or value in {".", ".."} or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            f"{field} must be a non-empty identifier using letters, numbers, '.', '_', or '-'"
        )
