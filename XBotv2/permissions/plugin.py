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
from XBotv2.core.errors import OperationError
from XBotv2.core.tools import ToolCall
from XBotv2.permissions.guard import make_permission_guard
from XBotv2.permissions.commands import PERMISSIONS_COMMANDS
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


def reload_live_policies(ctx: Any) -> None:
    """Rebuild active permission and sandbox objects after config changes."""
    services = ctx.services
    definition = services.agents.active_definition()
    base_config = services.agents.runtime_config(definition)
    _apply_live_policies(ctx, base_config)
    if definition is not None:
        ctx.services.permissions.configure_agent(definition.permissions)


def _apply_live_policies(ctx: Any, config: Any) -> None:
    """Apply one already-resolved policy to live runtime objects."""
    ctx.services.permissions.replace_rules(config.permissions)
    ctx.services.sandbox.replace_config(config.sandbox)


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
        overlay = normalize_agent_permissions(overlay)
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

    async def update_session_policy(
        self,
        *,
        paths: Any,
        session_id: str,
        contexts: list[Any],
        permissions: dict[str, str] | None = None,
        remove_permissions: list[str] | None = None,
        sandbox: dict[str, Any] | None = None,
        remove_sandbox: list[str] | None = None,
    ) -> dict[str, Any]:
        """Persist a session policy patch and apply it to every live thread."""
        from contextlib import AsyncExitStack

        for ctx in contexts:
            if ctx.turn_lock.locked():
                raise OperationError(
                    "thread_busy",
                    "Cannot update session policy while a turn is active.",
                    retryable=True,
                )
            registry = ctx.services.get("jobs")
            if registry is not None and registry.is_busy():
                raise OperationError(
                    "thread_busy",
                    "Cannot update session policy while a background task "
                    "is active.",
                    retryable=True,
                )
        async with AsyncExitStack() as stack:
            for ctx in sorted(contexts, key=lambda item: item.thread_id):
                await stack.enter_async_context(ctx.turn_lock)
            if not contexts:
                raise OperationError(
                    "thread_not_active",
                    "Session policy updates require one active thread.",
                )
            policy = contexts[0].services.settings.patch_session_policy(
                session_id=session_id,
                permissions=permissions,
                remove_permissions=remove_permissions or (),
                sandbox=sandbox,
                remove_sandbox=remove_sandbox or (),
            )
            for ctx in contexts:
                reload_live_policies(ctx)
        return policy

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
    inject = ["session", "tools", "approval", "variables", "commands"]
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
        for command in PERMISSIONS_COMMANDS:
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
