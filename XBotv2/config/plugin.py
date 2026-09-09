"""Config component: the configuration parsing service (``ctx.config``)."""

from __future__ import annotations

from collections.abc import Mapping
from pydantic import JsonValue
from xcore import Context

from XBotv2.config.contracts import UserContext
from XBotv2.config.service import ConfigService
from XBotv2.config.contracts import GET_POLICY, UPDATE_POLICY
from XBotv2.core.operations import EmptyRequest
from XBotv2.config.protocol import build_router
from XBotv2.server import contribute_router


async def mount_http(ctx: Context) -> None:
    await contribute_router(
        ctx,
        owner="xbot.config.http",
        router=build_router(sessions=ctx.sessions, paths=ctx.runtime_paths),
    )


class ConfigRuntimeComponent:
    """Register the path-bound config reader as ``ctx.settings``.

    The user context comes from this plugin's tree config (``user`` block),
    not a separate ``user.yaml`` document — consistent with the plugin-tree
    configuration model (``xcore.yaml`` + overlays).
    """

    name = "xbot.config"
    inject = ["runtime_log", "runtime_paths", "session_launch"]

    def apply(
        self,
        ctx: Context,
        config: Mapping[str, JsonValue] | None = None,
    ) -> None:
        config = config or {}
        user = UserContext.model_validate(config.get("user") or {})
        settings = ConfigService(
            ctx.runtime_paths,
            session_id=ctx.session_launch.session_id,
            workspace_root=ctx.session_launch.workspace_root,
            events=ctx,
            user_context=user,
            runtime_log=ctx.runtime_log,
        )
        ctx.set("settings", settings)
        operations = ConfigOperations(settings)
        ctx.on(GET_POLICY.name, operations.get_policy)
        ctx.on(UPDATE_POLICY.name, settings.update_policy)


class ConfigOperations:
    def __init__(self, settings: ConfigService) -> None:
        self._settings = settings

    def get_policy(self, _request: EmptyRequest):
        return self._settings.policy()


class ConfigPlugin:
    """Compose session-local settings and their HTTP projection."""

    name = "xbot.config"

    async def apply(
        self,
        ctx: Context,
        config: Mapping[str, JsonValue] | None = None,
    ) -> None:
        await ctx.plugin(ConfigRuntimeComponent(), config)
        await ctx.inject(["server", "sessions", "runtime_paths"], mount_http)


plugin = ConfigPlugin()

__all__ = ["ConfigPlugin"]
