"""Start one XBot application and own its complete composition lifecycle.

This module is the application boundary corresponding to DSH's app boot: it
creates the root context, publishes launcher-owned entry services, mounts and
settles the configured plugin tree, and dispatches the public Agent initialize
operation. It disposes partial state when
startup fails; it does not construct an Engine or implement Agent policy.
"""

from __future__ import annotations

import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from xcore import Context

from XBotv2.application.boot import boot_application
from XBotv2.application.child import ChildApplications
from XBotv2.application.client_events import ClientEventRouter
from XBotv2.application.host import mounted_application
from XBotv2.application.services import (
    AgentApplicationPort,
    ParentPermissions,
    SessionLaunch,
)
from XBotv2.config.seed import ensure_initial_config
from XBotv2.application.tree import load_agent_tree
from XBotv2.agents import AgentCreateOptions, AgentDefinition
from XBotv2.core.operations import dispatch_operation
from XBotv2.agents import INITIALIZE_AGENT
from XBotv2.session import AgentApplicationOptions

_IDENTIFIER_RE = __import__("re").compile(r"^[A-Za-z0-9._-]+$")


async def start_application(
    *,
    paths,
    provider_name: str = "default",
    session_id: str | None = None,
    thread_id: str = "agent",
    workspace_root: Path | str | None = None,
    plugin_dirs: list[Path | str] | None = None,
    llm_override=None,
    selected_agent: str | None = None,
    agent_definition: AgentDefinition | None = None,
    parent_permission_system=None,
    parent_thread_id: str = "",
    is_subagent: bool = False,
    interactive: bool = True,
    extra_plugins: list[dict[str, Any]] | None = None,
    client_events=None,
) -> Context:
    """Assemble the XBot runtime on an XCore context.

    Returns the owning XCore application context. Consumers obtain the loop
    driver from ``ctx.engine``; lifecycle and plugin services remain on the
    application instead of leaking through Engine. ``extra_plugins`` appends
    session-scoped plugin entries."""
    _validate_identifier("provider_name", provider_name)
    session_id = session_id or _new_session_id()
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
        plugin_dirs=plugin_dirs,
        extra_plugins=extra_plugins,
    )

    children = ChildApplications(
        paths=paths,
        provider_name=provider_name,
        session_id=session_id,
        workspace_root=workspace_root,
        plugin_dirs=plugin_dirs,
        llm_override=llm_override,
        parent_thread_id=thread_id,
        interactive=interactive,
        session_paths=session_paths,
    )

    def prepare(ctx: Any) -> None:
        ctx.set("runtime_paths", paths)
        ctx.set("session_launch", SessionLaunch(
            session_id=session_id,
            thread_id=thread_id,
            workspace_root=workspace_root,
            provider_name=provider_name,
            session_paths=session_paths,
            interactive=interactive,
            is_subagent=is_subagent,
        ))
        ctx.set("parent_permissions", ParentPermissions(parent_permission_system))
        ctx.set("client_events", ClientEventRouter(parent=client_events))
        ctx.set("child_applications", children)

    try:
        plugin_ctx = await boot_application(
            tree=tree,
            data_dir=thread_paths.state_dir,
            plugin_dirs=plugin_dirs,
            prepare=prepare,
        )
        await dispatch_operation(
            plugin_ctx,
            INITIALIZE_AGENT,
            AgentCreateOptions(
                session_id=session_id,
                thread_id=thread_id,
                workspace_root=str(workspace_root),
                provider_name=provider_name,
                agent_definition=agent_definition,
                model_override=llm_override,
                selected_agent=selected_agent,
                parent_thread_id=parent_thread_id,
                is_subagent=is_subagent,
            ),
        )
        await plugin_ctx.settle()

        return plugin_ctx
    except BaseException as startup_error:
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
            {"id": name, "name": name, "config": config}
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
        plugin_dirs=[] if options.no_plugins else None,
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


def _new_session_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
