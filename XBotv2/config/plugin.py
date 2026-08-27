"""Config component: the configuration parsing service (``ctx.config``)."""

from __future__ import annotations

from typing import Any

from XBotv2.config.models import UserContext
from XBotv2.config.service import ConfigService
from XBotv2.config.contracts import GET_POLICY, UPDATE_POLICY
from XBotv2.core.operations import EmptyRequest
from XBotv2.core.runtime_logging import RuntimeLog
from XBotv2.permissions import PERMISSION_DECIDED, PermissionDecided


class ConfigComponent:
    """Register the path-bound config reader as ``ctx.settings``.

    The user context comes from this plugin's tree config (``user`` block),
    not a separate ``user.yaml`` document — consistent with the plugin-tree
    configuration model (``xcore.yaml`` + overlays).
    """

    name = "xbot.config"
    inject = ["runtime_log", "runtime_paths", "session_launch"]

    def apply(self, ctx: Any, config: Any = None) -> None:
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
        persister = PermissionRulePersister(
            paths=ctx.runtime_paths,
            session_id=ctx.session_launch.session_id,
            runtime_log=ctx.runtime_log,
        )
        ctx.on(PERMISSION_DECIDED, persister.persist)


class PermissionRulePersister:
    def __init__(
        self,
        *,
        paths: Any,
        session_id: str,
        runtime_log: RuntimeLog,
    ) -> None:
        self._paths = paths
        self._session_id = session_id
        self._log = runtime_log.bind("config", session_id=session_id)

    async def persist(self, event: PermissionDecided) -> None:
        from XBotv2.config.policy import persist_permission_rule

        persist_permission_rule(
            paths=self._paths,
            session_id=self._session_id,
            rule=event.rule,
            decision=event.decision,
            scope=event.scope,
        )
        self._log.info(
            "config.permission.persisted",
            tool=event.rule.get("tool", ""),
            decision=event.decision,
            scope=event.scope,
        )


class ConfigOperations:
    def __init__(self, settings: ConfigService) -> None:
        self._settings = settings

    def get_policy(self, _request: EmptyRequest):
        return self._settings.policy()


plugin = ConfigComponent()
