"""Session settings and independently mounted configuration HTTP routes."""

from __future__ import annotations

from xcore.api.context import Context
from XBotv2.config.api.contracts import (
    ConfigPluginConfig,
    GET_POLICY,
    UPDATE_POLICY,
    SETTINGS,
)
from XBotv2.config.impl.service import ConfigService
from XBotv2.config.impl.seed import ensure_initial_config
from XBotv2.loader.api.service import LOADER
from XBotv2.core.api.operations import EmptyRequest
from XBotv2.config.impl.router import build_router
from XBotv2.server.api.contracts import contribute_router
from XBotv2.session.api.contracts import SESSIONS


async def mount_http(ctx: Context) -> None:
    await contribute_router(
        ctx,
        owner="xbot.config.http",
        router=build_router(
            sessions=ctx.sessions, paths=ctx.runtime_paths, loader=ctx.require(LOADER)
        ),
    )


def mount_settings(ctx: Context) -> None:
    """Mount policy operations only in an actual Agent session."""
    settings = ConfigService(
        ctx.runtime_paths,
        session_id=ctx.thread_info.session_id,
        thread_id=ctx.thread_info.thread_id,
        workspace_root=ctx.thread_info.workspace_root,
        events=ctx,
        runtime_log=ctx.runtime_log,
        extra_plugins=ctx.plugin_overrides,
        plugin_dirs=ctx.plugin_dirs,
        no_plugins=ctx.no_plugins,
    )
    ctx.set(SETTINGS, settings)
    operations = ConfigOperations(settings)
    ctx.on(GET_POLICY.name, operations.get_policy)
    ctx.on(UPDATE_POLICY.name, settings.update_policy)


def mount_process(ctx, config):
    from XBotv2.session.api.storage import FORK_COPY_STATE
    from XBotv2.persistence.api.contracts import PERSISTENCE

    async def copy_state(event):
        await ctx.require(PERSISTENCE).copy_document(
            event.source_session_id, event.target_session_id, "config.yaml"
        )
        event.handled.add("config")

    ctx.on(FORK_COPY_STATE, copy_state)
    ensure_initial_config(ctx.runtime_paths)
    ctx.inject(["server", "sessions", "runtime_paths", LOADER], mount_http)


from XBotv2.persistence.api.contracts import PERSISTENCE

mount_process.inject = ("runtime_paths", PERSISTENCE)
mount_process.provide = ()
mount_process.Config = ConfigPluginConfig


def mount_thread(ctx, config):
    mount_settings(ctx)


mount_thread.inject = (
    "runtime_log",
    "runtime_paths",
    "thread_info",
    SESSIONS,
    "plugin_overrides",
    "plugin_dirs",
    "no_plugins",
)
mount_thread.provide = (SETTINGS,)
mount_thread.Config = ConfigPluginConfig


class ConfigOperations:
    def __init__(self, settings: ConfigService) -> None:
        self._settings = settings

    def get_policy(self, _request: EmptyRequest):
        return self._settings.policy()
