"""Permissions component: the permission system as an XCore service.

Decides tool-call allow/ask/deny against runtime permission rules and, for a
child Agent, an optional parent session permission system (intersection).
The system registers itself as a monotonic execution guard on ``ctx.tools``
(see :meth:`ToolsService.guard`), so the tool pipeline gates calls through
it without importing or depending on this plugin.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import asyncio
from pydantic import JsonValue, TypeAdapter
from xcore import Context
from xcore.state import StateService

from XBotv2.agents import AGENT_CONFIGURED, AgentConfigured, AgentDefinition
from XBotv2.application import APPLICATION_INITIALIZED, ApplicationInitialized
from XBotv2.config import POLICY_CHANGED, PolicyChanged
from XBotv2.core.tools import ClientEvent, ToolCall
from XBotv2.core.variables import RuntimeVariables
from XBotv2.permissions.contracts import (
    PermissionConfig,
    PermissionDecision,
    PermissionRuleConfig,
    PermissionsPort,
)
from XBotv2.permissions import PERMISSION_DECIDED, PermissionDecided
from XBotv2.permissions.guard import PermissionGuard
from XBotv2.permissions.commands import build_permissions_commands
from XBotv2.permissions.rules import (
    permission_rule_for_tool_call,
    requested_permission_rule,
)
from XBotv2.permissions.system import (
    PermissionSystem,
    normalize_agent_permissions,
)
from XBotv2.permissions.protocol import ApprovalDecision, PermissionRequestData
from XBotv2.permissions.approval import ApprovalService
from XBotv2.agentloop import Events

class PermissionsService(PermissionsPort):
    """Stable plugin capability whose concrete policy remains plugin-owned."""

    def __init__(
        self,
        config: PermissionConfig | dict[str, JsonValue],
        variables: RuntimeVariables,
        store: StateService,
        parent: PermissionsPort | None = None,
    ) -> None:
        self._base_config = PermissionConfig.model_validate(config)
        self._agent_overlay: dict[str, JsonValue] | None = None
        self._grants: list[PermissionRuleConfig] = []
        self._store = store
        self._lock = asyncio.Lock()
        self._system = PermissionSystem(variables=variables, parent=parent)
        self._rebuild()

    def _rebuild(self) -> None:
        overlay = normalize_agent_permissions(self._agent_overlay)
        merged = {
            "deny": [
                *list(overlay.get("deny") or []),
                *[rule.model_dump(exclude_none=True) for rule in self._base_config.deny],
            ],
            "allow": [
                *list(overlay.get("allow") or []),
                *[rule.model_dump(exclude_none=True) for rule in self._base_config.allow],
            ],
            "ask": [
                *list(overlay.get("ask") or []),
                *[rule.model_dump(exclude_none=True) for rule in self._base_config.ask],
            ],
        }
        merged["allow"] = [
            *[grant.model_dump(exclude_none=True) for grant in self._grants],
            *merged["allow"],
        ]
        self._system.replace_rules(merged)

    def configure_agent(
        self,
        overlay: dict[str, JsonValue],
    ) -> None:
        self._agent_overlay = overlay
        self._rebuild()

    def replace_rules(self, config: PermissionConfig) -> None:
        self._base_config = PermissionConfig.model_validate(config)
        self._rebuild()

    def check(
        self,
        tool_name: str,
        args: dict[str, JsonValue] | None = None,
    ) -> PermissionDecision:
        return self._system.check(tool_name, args)

    def explicit_allow(
        self,
        tool_name: str,
        args: dict[str, JsonValue] | None = None,
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

    def consume_once(self, tool_name: str, args: dict[str, JsonValue]) -> None:
        self._system.consume_once(tool_name, args)

    def rule_for_call(self, call: ToolCall) -> dict[str, JsonValue]:
        return permission_rule_for_tool_call(
            call, workspace=self._system.variables.get("workspace"),
        )

    def _replace_grants(self, rules: list[PermissionRuleConfig]) -> None:
        self._grants = rules
        self._rebuild()

    def session_grants(self) -> tuple[PermissionRuleConfig, ...]:
        return tuple(self._grants)

    async def restore(self) -> None:
        if set(await self._store.keys()) - {"grants"}:
            raise ValueError("Unsupported permission state layout; expected grants snapshot")
        rules = TypeAdapter(list[PermissionRuleConfig]).validate_python(
            await self._store.get("grants", []),
        )
        self._replace_grants(rules)

    async def grant_session(
        self,
        rule: PermissionRuleConfig | dict[str, JsonValue],
    ) -> None:
        rule = PermissionRuleConfig.model_validate(rule)
        async with self._lock:
            if rule in self._grants:
                return
            updated = [*self._grants, rule]
            await self._store.set(
                "grants",
                [item.model_dump(exclude_none=True) for item in updated],
            )
            self._replace_grants(updated)

    async def revoke_session(self, index: int) -> None:
        async with self._lock:
            if index < 1 or index > len(self._grants):
                raise ValueError(f"Grant index must be between 1 and {len(self._grants)}")
            updated = [*self._grants]
            updated.pop(index - 1)
            await self._store.set(
                "grants",
                [item.model_dump(exclude_none=True) for item in updated],
            )
            self._replace_grants(updated)

    async def clear_session_grants(self) -> int:
        async with self._lock:
            count = len(self._grants)
            if count:
                await self._store.set("grants", [])
                self._replace_grants([])
            return count


class PermissionsComponent:
    inject = [
        "session",
        "session_launch",
        "parent_permissions",
        "tools",
        "client_events",
        "interactions",
        "variables",
        "commands",
        "settings",
        "state",
    ]
    """Register the permission system as ``ctx.permissions`` and its guard."""

    name = "xbot.permissions"
    Config = PermissionConfig

    def apply(self, ctx: Context, config: PermissionConfig) -> None:
        approval = ApprovalService(
            ctx,
            ctx.client_events,
            ctx.interactions.create_waiter(),
        )
        ctx.set("approval", approval)
        ctx.dispose(ctx.client_events.register_waiter("permission_request", approval.waiter))
        ctx.on(Events.SESSION_CLOSE, approval.session_closed)
        permissions = PermissionsService(
            config,
            ctx.variables,
            ctx.state.namespace("permissions"),
            parent=ctx.parent_permissions.value,
        )
        ctx.set("permissions", permissions)
        for command in build_permissions_commands(ctx.settings, permissions):
            ctx.commands.register(command)
        handlers = PermissionHandlers(permissions, ctx.emit)

        guard = PermissionGuard(
            permissions,
            approval,
            ctx.emit,
            handlers.apply_decision,
        )
        ctx.tools.guard(guard.check)
        ctx.on(APPLICATION_INITIALIZED, handlers.configure_initial, prepend=True)
        ctx.on(AGENT_CONFIGURED, handlers.configure_agent, prepend=True)
        ctx.on(POLICY_CHANGED, handlers.update_policy)

        if ctx.session_launch.interactive:
            from XBotv2.permissions.tools import RequestPermissionTool

            tool = RequestPermissionTool(
                approval,
                handlers.apply_decision,
            )
            ctx.tools.register(tool.as_tool())


class PermissionHandlers:
    def __init__(
        self,
        permissions: PermissionsService,
        emit: Callable[[str, object], Awaitable[object]],
    ) -> None:
        self._permissions = permissions
        self._emit = emit

    async def apply_decision(
        self,
        event: ClientEvent,
        decision: ApprovalDecision,
    ) -> ApprovalDecision:
        data = PermissionRequestData.model_validate(event.data)
        if data.permission is not None:
            rule = requested_permission_rule(data.permission.model_dump())
        else:
            call = data.tool_call
            rule = self._permissions.rule_for_call(call)
            if decision.decision == "allow" and self._permissions.check(call.name, call.args) == "deny":
                decision = ApprovalDecision(decision="deny", scope=decision.scope)
        rule = PermissionRuleConfig.model_validate(rule)
        if decision.decision == "allow":
            if decision.scope == "session":
                await self._permissions.grant_session(rule)
            elif data.permission is not None:
                self._permissions.grant_once(data.permission.tool, data.permission.params)
        await self._emit(
            PERMISSION_DECIDED,
            PermissionDecided(
                decision=decision.decision, scope=decision.scope, rule=rule,
                request_id=data.request_id, source=data.source,
            ),
        )
        return decision

    async def configure_initial(self, event: ApplicationInitialized) -> None:
        await self._permissions.restore()
        self._configure_agent(event.agent)

    async def configure_agent(self, event: AgentConfigured) -> None:
        self._configure_agent(event.agent)

    def _configure_agent(self, agent: AgentDefinition | None) -> None:
        if agent is not None:
            self._permissions.configure_agent(agent.permissions)

    async def update_policy(self, event: PolicyChanged) -> None:
        self._permissions.replace_rules(
            PermissionConfig.model_validate(event.effective_permissions)
        )


plugin = PermissionsComponent()
