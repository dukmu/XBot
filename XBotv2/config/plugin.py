"""Config component: the configuration parsing service (``ctx.config``)."""

from __future__ import annotations

from typing import Any

from XBotv2.config.models import UserContext
from XBotv2.config.service import ConfigService
from XBotv2.agentloop import Events
from XBotv2.config.contracts import GET_POLICY, UPDATE_POLICY
from XBotv2.core.operations import EmptyRequest


class ConfigComponent:
    """Register the path-bound config reader as ``ctx.settings``.

    The user context comes from this plugin's tree config (``user`` block),
    not a separate ``user.yaml`` document — consistent with the plugin-tree
    configuration model (``xcore.yaml`` + overlays).
    """

    name = "xbot.config"
    inject = ["runtime_paths", "session_launch"]

    def apply(self, ctx: Any, config: Any = None) -> None:
        config = config or {}
        user = UserContext.model_validate(config.get("user") or {})
        settings = ConfigService(
            ctx.runtime_paths,
            session_id=ctx.session_launch.session_id,
            workspace_root=ctx.session_launch.workspace_root,
            events=ctx,
            user_context=user,
        )
        ctx.set("settings", settings)
        ctx.on(GET_POLICY.name, lambda _request: settings.policy())
        ctx.on(UPDATE_POLICY.name, settings.update_policy)

        async def persist_permission(event: Any) -> None:
            details = event.event if isinstance(event.event, dict) else {}
            rule = details.get("rule")
            if not rule:
                return
            from XBotv2.config.policy import persist_permission_rule

            persist_permission_rule(
                paths=ctx.runtime_paths,
                session_id=ctx.session_launch.session_id,
                rule=rule,
                decision=str(details.get("decision") or ""),
                scope=str(details.get("scope") or "once"),
            )

        ctx.on(Events.PERMISSION_DECIDED, persist_permission)


plugin = ConfigComponent()
