"""Permissions component: the permission system as an XCore service.

Decides tool-call allow/ask/deny against the runtime permission rules and
(optionally) a parent session's permission system (subagent intersection).
The system registers itself as a monotonic execution guard on ``ctx.tools``
(see :meth:`ToolsService.guard`), so the tool pipeline gates calls through
it without importing or depending on this plugin.
"""

from __future__ import annotations

from typing import Any

from XBotv2.core.events import EventContext, Events
from XBotv2.core.tools import ToolCall
from XBotv2.permissions.guard import make_permission_guard
from XBotv2.permissions.rules import (
    permission_rule_for_tool_call,
    requested_permission_rule,
)
from XBotv2.permissions.system import PermissionIntersection, PermissionSystem

_KEEP_PARENT = object()


class PermissionsService:
    """Stable plugin capability whose concrete policy remains plugin-owned."""

    def __init__(self, config: Any, variables: Any, parent: Any = None) -> None:
        self._variables = variables
        self._base_config = self._as_dict(config)
        self._system: Any = None
        self.configure(config, parent=parent)

    @property
    def config(self) -> Any:
        target = getattr(self._system, "child", self._system)
        return getattr(target, "config", None)

    def configure(self, config: Any, *, parent: Any = None) -> None:
        self._parent = parent
        child = PermissionSystem(config, variables=self._variables)
        parent_system = getattr(parent, "_system", parent)
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
        overlay = self._as_dict(overlay)
        merged = {
            decision: [
                *list(overlay.get(decision) or []),
                *list(self._base_config.get(decision) or []),
            ]
            for decision in ("deny", "allow", "ask")
        }
        self.configure(
            {key: value for key, value in merged.items() if value},
            parent=self._parent if parent is _KEEP_PARENT else parent,
        )

    def replace_rules(self, config: Any) -> None:
        self._base_config = self._as_dict(config)
        self.configure(self._base_config, parent=self._parent)

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

    def __getattr__(self, name: str) -> Any:
        return getattr(self._system, name)


class PermissionsComponent:
    inject = ["session", "tools", "approval"]
    """Register the permission system as ``ctx.permissions`` and its guard."""

    name = "xbot.permissions"

    def apply(self, ctx: Any, config: Any = None) -> None:
        config = config or {}
        permissions = PermissionsService(
            config.get("permissions"),
            ctx.variables,
            parent=config.get("parent_permission_system"),
        )
        ctx.set("permissions", permissions)

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
                    client_event=event,
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

        if bool(config.get("interactive", True)):
            from XBotv2.permissions.tools import request_permission

            ctx.tools.register(request_permission, injected={
                "permissions": permissions,
                "approval": ctx.approval,
                "record_permission_decision": record_permission_decision,
            })


plugin = PermissionsComponent()
