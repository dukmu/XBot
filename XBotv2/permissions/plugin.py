"""Permissions component: the permission system as an XCore service.

Decides tool-call allow/ask/deny against runtime permission rules and, for a
child Agent, an optional parent session permission system (intersection).
The system registers itself as a monotonic execution guard on ``ctx.tools``
(see :meth:`ToolsService.guard`), so the tool pipeline gates calls through
it without importing or depending on this plugin.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from pydantic import BaseModel

from XBotv2.agents import AGENT_CONFIGURED, AgentConfigured
from XBotv2.application import APPLICATION_INITIALIZED, ApplicationInitialized
from XBotv2.config import POLICY_CHANGED, PolicyChanged
from XBotv2.agentloop import EventContext, Events
from XBotv2.core.tools import ToolCall
from XBotv2.permissions import PERMISSION_DECIDED, PermissionDecided
from XBotv2.permissions.guard import PermissionGuard
from XBotv2.permissions.commands import build_permissions_commands
from XBotv2.permissions.rules import (
    permission_rule_for_tool_call,
    requested_permission_rule,
)
from XBotv2.permissions.system import PermissionSystem, normalize_agent_permissions

_KEEP_PARENT = object()


class PermissionsService:
    """Stable plugin capability whose concrete policy remains plugin-owned."""

    def __init__(self, config: object, variables: Any, parent: Any = None) -> None:
        self._variables = variables
        self._base_config = self._as_dict(config)
        self._agent_overlay: Any = None
        self._parent = parent
        self._system: PermissionSystem
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
            parent=self._parent,
        )
        self._system = child

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

    def replace_rules(self, config: object) -> None:
        self._base_config = self._as_dict(config)
        self._rebuild()

    @staticmethod
    def _as_dict(value: object) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, BaseModel):
            return dict(value.model_dump(exclude_none=True))
        if isinstance(value, Mapping):
            return dict(value)
        raise TypeError("Permission configuration must be a mapping")

    def add_rule(self, decision: str, rule: dict[str, Any]) -> None:
        self._system.add_rule(decision, rule)

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
        handlers = PermissionHandlers(permissions, ctx.emit)

        guard = PermissionGuard(
            permissions,
            ctx.approval,
            ctx.emit,
            handlers.record_decision,
        )
        ctx.tools.guard(guard.check)
        ctx.on(APPLICATION_INITIALIZED, handlers.configure_initial, prepend=True)
        ctx.on(AGENT_CONFIGURED, handlers.configure_agent, prepend=True)
        ctx.on(POLICY_CHANGED, handlers.update_policy)

        if ctx.session_launch.interactive:
            from XBotv2.permissions.tools import RequestPermissionTool

            tool = RequestPermissionTool(
                permissions,
                ctx.approval,
                handlers.record_decision,
            )
            ctx.tools.register(tool.as_tool())


class PermissionHandlers:
    def __init__(
        self,
        permissions: PermissionsService,
        emit: Callable[[str, Any], Awaitable[Any]],
    ) -> None:
        self._permissions = permissions
        self._emit = emit

    async def record_decision(
        self,
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
            self._permissions.add_rule(decision, rule)
        await self._emit(
            PERMISSION_DECIDED,
            PermissionDecided(decision=decision, scope=scope, rule=rule),
        )

    async def configure_initial(self, event: ApplicationInitialized) -> None:
        if event.agent is not None:
            self._permissions.configure_agent(event.agent.permissions)

    async def configure_agent(self, event: AgentConfigured) -> None:
        if event.agent is not None:
            self._permissions.configure_agent(event.agent.permissions)

    async def update_policy(self, event: PolicyChanged) -> None:
        self._permissions.replace_rules(event.config.permissions)


plugin = PermissionsComponent()
