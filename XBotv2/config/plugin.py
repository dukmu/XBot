"""Config component: the configuration parsing service (``ctx.config``)."""

from __future__ import annotations

from xcore import Context

from XBotv2.config.contracts import ConfigPluginConfig
from XBotv2.config.service import ConfigService
from XBotv2.config.contracts import GET_POLICY, UPDATE_POLICY
from XBotv2.core.operations import EmptyRequest
from XBotv2.config.protocol import build_router
from XBotv2.server import contribute_router


async def mount_http(ctx: Context) -> None:
    await contribute_router(
        ctx,
        owner="xbot.config.http",
        router=build_router(sessions=ctx.sessions, settings=ctx.settings),
    )


class ConfigPlugin:
    """Provide session-bound settings and the configuration HTTP routes."""

    name = "xbot.config"
    Config = ConfigPluginConfig
    inject = [
        "runtime_log", "runtime_paths", "session_launch",
        "plugin_overrides", "plugin_dirs",
        "no_plugins",
    ]

    def apply(
        self,
        ctx: Context,
        config: ConfigPluginConfig,
    ) -> None:
        settings = ConfigService(
            ctx.runtime_paths,
            session_id=ctx.session_launch.session_id,
            workspace_root=ctx.session_launch.workspace_root,
            events=ctx,
            user_context=config.user,
            runtime_log=ctx.runtime_log,
            extra_plugins=ctx.plugin_overrides,
            plugin_dirs=ctx.plugin_dirs,
            is_subagent=ctx.session_launch.is_subagent,
            no_plugins=ctx.no_plugins,
        )
        ctx.set("settings", settings)
        operations = ConfigOperations(settings)
        ctx.on(GET_POLICY.name, operations.get_policy)
        ctx.on(UPDATE_POLICY.name, settings.update_policy)
        ctx.inject(["server", "sessions", "settings"], mount_http)


class ConfigOperations:
    def __init__(self, settings: ConfigService) -> None:
        self._settings = settings

    def get_policy(self, _request: EmptyRequest):
        return self._settings.policy()


plugin = ConfigPlugin()

__all__ = ["ConfigPlugin"]
