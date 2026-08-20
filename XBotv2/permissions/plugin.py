"""Permissions component: the permission system as an XCore service.

Decides tool-call allow/ask/deny against the runtime permission rules and
(optionally) a parent session's permission system (subagent intersection).
The system registers itself as a monotonic execution guard on ``ctx.tools``
(see :meth:`ToolsService.guard`), so the tool pipeline gates calls through
it without importing or depending on this plugin.
"""

from __future__ import annotations

from typing import Any

from XBotv2.config.events import POLICY_CHANGED, PolicyChanged
from XBotv2.core.events import EventContext, Events
from XBotv2.core.tools import ClientEvent, ToolCall
from XBotv2.permissions.guard import make_permission_guard
from XBotv2.permissions.commands import build_permissions_commands
from XBotv2.permissions.rules import (
    permission_rule_for_tool_call,
    requested_permission_rule,
)
from XBotv2.permissions.system import (
    PermissionIntersection,
    PermissionSystem,
    normalize_agent_permissions,
)

_KEEP_PARENT = object()


class PermissionsService:
    """Stable plugin capability whose concrete policy remains plugin-owned."""

    def __init__(self, config: Any, variables: Any, parent: Any = None) -> None:
        self._variables = variables
        self._base_config = self._as_dict(config)
        self._agent_overlay: Any = None
        self._parent = parent
        self._system: Any = None
        self._rebuild()

    @property
    def config(self) -> Any:
        target = getattr(self._system, "child", self._system)
        return getattr(target, "config", None)

    def configure(self, config: Any, *, parent: Any = None) -> None:
        self._base_config = self._as_dict(config)
        self._parent = parent
        self._agent_overlay = None
        self._rebuild()

    def _rebuild(self) -> None:
        overlay = normalize_agent_permissions(self._agent_overlay)
        merged = {
            decision: [
                *list(overlay.get(decision) or []),
                *list(self._base_config.get(decision) or []),
            ]
            for decision in ("deny", "allow", "ask")
        }
        child = PermissionSystem(
            {key: value for key, value in merged.items() if value},
            variables=self._variables,
        )
        parent_system = self._parent
        self._system = (
            PermissionIntersection(parent_system, child)
            if parent_system is not None
            else child
        )

    def configure_agent(
        self,
        overlay: Any,
        *,
        parent: Any = _KEEP_PARENT,
    ) -> None:
        self._agent_overlay = overlay
        if parent is not _KEEP_PARENT:
            self._parent = parent
        self._rebuild()

    def replace_rules(self, config: Any) -> None:
        self._base_config = self._as_dict(config)
        self._rebuild()

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if hasattr(value, "model_dump"):
            return dict(value.model_dump(exclude_none=True))
        return dict(value)

    def add_rule(self, decision: str, rule: dict[str, Any]) -> None:
        target = getattr(self._system, "child", self._system)
        target.add_rule(decision, rule)

    def check(self, tool_name: str, args: dict[str, Any] | None = None) -> str:
        return self._system.check(tool_name, args)

    def explicit_allow(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        constrain_param: str | None = None,
    ) -> bool:
        return self._system.explicit_allow(
            tool_name,
            args,
            constrain_param=constrain_param,
        )

    def check_tool_call(self, tool_call: ToolCall) -> tuple[str, str]:
        return self._system.check_tool_call(tool_call)

    def grant_once(self, tool_name: str, param_patterns: dict[str, str]) -> None:
        self._system.grant_once(tool_name, param_patterns)


class PermissionsComponent:
    inject = [
        "session",
        "session_launch",
        "parent_permissions",
        "tools",
        "approval",
        "variables",
        "commands",
        "settings",
    ]
    """Register the permission system as ``ctx.permissions`` and its guard."""

    name = "xbot.permissions"

    def apply(self, ctx: Any, config: Any = None) -> None:
        config = config or {}
        permissions = PermissionsService(
            config.get("permissions"),
            ctx.variables,
            parent=ctx.parent_permissions.value,
        )
        ctx.set("permissions", permissions)
        for command in build_permissions_commands(ctx.settings):
            ctx.commands.register(command)

        async def record_permission_decision(
            event: dict[str, Any],
            decision: str,
            scope: str,
        ) -> None:
            data = event.get("data") or {}
            if data.get("source") == "request_permission":
                rule = requested_permission_rule(data.get("permission"))
            else:
                rule = permission_rule_for_tool_call(
                    ToolCall.from_dict(dict(data.get("tool_call") or {}))
                )
            if not rule:
                return
            if scope == "session":
                permissions.add_rule(decision, rule)
            await ctx.emit(
                Events.PERMISSION_DECIDED,
                EventContext(
                    client_event=ClientEvent.from_mapping(event),
                    event={
                        "decision": decision,
                        "scope": scope,
                        "rule": rule,
                    },
                ),
            )

        ctx.tools.guard(make_permission_guard(
            permissions,
            ctx.approval,
            ctx.emit,
            record_decision=record_permission_decision,
        ))

        async def configure_agent(event: EventContext) -> None:
            if event.agent is not None:
                permissions.configure_agent(event.agent.permissions)

        ctx.on(Events.SESSION_INIT, configure_agent, prepend=True)
        ctx.on(Events.AGENT_CONFIGURED, configure_agent, prepend=True)

        async def update_policy(event: PolicyChanged) -> None:
            permissions.replace_rules(event.config.permissions)

        ctx.on(POLICY_CHANGED, update_policy)

        if ctx.session_launch.interactive:
            from XBotv2.permissions.tools import request_permission

            ctx.tools.register(request_permission, injected={
                "permissions": permissions,
                "approval": ctx.approval,
                "record_permission_decision": record_permission_decision,
            })


plugin = PermissionsComponent()
