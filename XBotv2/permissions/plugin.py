"""Permissions component: the permission system as an XCore service.

Decides tool-call allow/ask/deny against runtime permission rules and, for a
child Agent, an optional parent session permission system (intersection).
The system registers itself as a monotonic execution guard on ``ctx.tools``
(see :meth:`ToolsService.guard`), so the tool pipeline gates calls through
it without importing or depending on this plugin.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
import asyncio
from pydantic import JsonValue, TypeAdapter
from xcore import Context
from xcore.state import StateService

from XBotv2.agents import (
    AGENT_CONFIGURED,
    AgentCatalogPort,
    AgentConfigured,
    AgentDefinition,
)
from XBotv2.application import APPLICATION_INITIALIZED, ApplicationInitialized
from XBotv2.config import POLICY_CHANGED, PolicyChanged
from XBotv2.core.tools import ToolCall
from XBotv2.core.variables import RuntimeVariables
from XBotv2.interactions import InteractionReceipt, InteractionRegistration
from XBotv2.permissions.contracts import (
    PermissionDecision,
    PermissionPolicy,
    PermissionRule,
    PermissionsPort,
)
from XBotv2.permissions import (
    Allowed,
    Approval,
    Denied,
    PERMISSION_DECIDED,
    PermissionDecisionRecorded,
)
from XBotv2.permissions.guard import PermissionGuard
from XBotv2.permissions.commands import build_permissions_commands
from XBotv2.permissions.rules import (
    permission_rule_for_tool_call,
    requested_permission_rule,
)
from XBotv2.permissions.system import PermissionSystem
from XBotv2.permissions.contracts import PermissionRequest, NamedPermission
from XBotv2.permissions.approval import ApprovalService
from XBotv2.permissions.protocol import PermissionResponseRecorded
from XBotv2.agentloop import Events
from XBotv2.agentloop.contracts import ToolsPort


def _permission_response_recorded(
    receipt: InteractionReceipt,
) -> PermissionResponseRecorded:
    approval = receipt.resolution
    if not isinstance(approval, (Allowed, Denied)):
        raise TypeError(
            f"Invalid permission resolution: {type(approval).__name__}"
        )
    return PermissionResponseRecorded(
        interaction_id=receipt.interaction_id,
        approval=approval,
        pending_ids=receipt.pending_ids,
    )

class PermissionsService(PermissionsPort):
    """Stable plugin capability whose concrete policy remains plugin-owned."""

    def __init__(
        self,
        policies: Sequence[PermissionPolicy],
        variables: RuntimeVariables,
        store: StateService,
        parent: PermissionsPort | None = None,
        tools: ToolsPort | None = None,
    ) -> None:
        self._base_policies = tuple(policies)
        self._agent_policy: PermissionPolicy | None = None
        self._grants: list[PermissionRule] = []
        self._store = store
        self._lock = asyncio.Lock()
        self._tools = tools
        self._system = PermissionSystem(variables=variables, parent=parent)
        self._rebuild()

    def _rebuild(self) -> None:
        policies = list(self._base_policies)
        if self._agent_policy is not None:
            policies.append(self._agent_policy)
        self._system.replace_policies(policies)
        self._system.replace_session_grants(self._grants)

    def configure_agent(
        self,
        policy: PermissionPolicy,
    ) -> None:
        self._agent_policy = policy
        self._rebuild()

    def replace_policies(self, policies: Sequence[PermissionPolicy]) -> None:
        self._base_policies = tuple(policies)
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

    def rule_for_call(
        self,
        call: ToolCall,
        *,
        decision: PermissionDecision,
    ) -> PermissionRule:
        return permission_rule_for_tool_call(
            call,
            workspace=self._system.variables.get("workspace"),
            # The tool owner declares which arguments define the grant scope;
            # permissions never keeps its own copy of that vocabulary.
            selectors=self._grant_selectors(call.name),
            decision=decision,
        )

    def _grant_selectors(self, tool_name: str) -> tuple[str, ...] | None:
        if self._tools is None or not tool_name:
            return None
        tool = self._tools.resolve(tool_name)
        if tool is None:
            return None
        return tuple(tool.grant_selectors) or None

    def _replace_grants(self, rules: list[PermissionRule]) -> None:
        self._grants = rules
        self._rebuild()

    def session_grants(self) -> tuple[PermissionRule, ...]:
        return tuple(self._grants)

    async def restore(self) -> None:
        if set(await self._store.keys()) - {"grants"}:
            raise ValueError("Unsupported permission state layout; expected grants snapshot")
        rules = TypeAdapter(list[PermissionRule]).validate_python(
            await self._store.get("grants", []),
        )
        self._replace_grants(rules)

    async def grant_session(
        self,
        rule: PermissionRule,
    ) -> None:
        if rule.decision != "allow":
            raise ValueError("A session grant must be an allow rule")
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
        "agent_catalog",
    ]
    """Register the permission system as ``ctx.permissions`` and its guard."""

    name = "xbot.permissions"
    Config = PermissionPolicy

    def apply(self, ctx: Context, config: PermissionPolicy) -> None:
        approval = ApprovalService(
            ctx,
            ctx.client_events,
            ctx.interactions.create_waiter(
                timed_out=lambda reason: Denied(reason=reason),
                cancelled=lambda reason: Denied(reason=reason),
            ),
        )
        ctx.set("approval", approval)
        ctx.dispose(ctx.client_events.register_interaction(
            InteractionRegistration(
                kind="permission_request",
                request_type=PermissionRequest,
                resolution_types=(Allowed, Denied),
                waiter=approval.waiter,
                timeout_seconds=lambda _request: None,
                recorded_event=_permission_response_recorded,
            )
        ))
        ctx.on(Events.SESSION_CLOSE, approval.session_closed)
        permissions = PermissionsService(
            ctx.settings.permission_policies(),
            ctx.variables,
            ctx.state.namespace("permissions"),
            parent=ctx.parent_permissions.value,
            tools=ctx.tools,
        )
        ctx.set("permissions", permissions)
        for command in build_permissions_commands(ctx.settings, permissions):
            ctx.commands.register(command)
        handlers = PermissionHandlers(permissions, ctx.emit, ctx.agent_catalog)

        guard = PermissionGuard(
            permissions,
            approval,
            ctx.emit,
            handlers.apply_decision,
        )
        ctx.tools.guard(guard.check, approval=True)
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
        agents: AgentCatalogPort,
    ) -> None:
        self._permissions = permissions
        self._emit = emit
        self._agents = agents

    async def apply_decision(
        self,
        event: PermissionRequest,
        decision: Approval,
    ) -> Approval:
        data = event.subject
        if not isinstance(data, NamedPermission):
            call = data.tool_call
            if isinstance(decision, Allowed) and self._permissions.check(call.name, call.args) == "deny":
                decision = Denied(reason="A deny rule overrides the approval")
        rule_decision: PermissionDecision = (
            "allow" if isinstance(decision, Allowed) else "deny"
        )
        rule = (
            requested_permission_rule(
                data.model_dump(),
                decision=rule_decision,
            )
            if isinstance(data, NamedPermission)
            else self._permissions.rule_for_call(
                data.tool_call,
                decision=rule_decision,
            )
        )
        if isinstance(decision, Allowed):
            if decision.scope == "session":
                await self._permissions.grant_session(rule)
            elif isinstance(data, NamedPermission):
                self._permissions.grant_once(data.tool, data.params)
        await self._emit(
            PERMISSION_DECIDED,
            PermissionDecisionRecorded(
                request=event,
                approval=decision,
                rule=rule,
            ),
        )
        return decision

    async def configure_initial(self, event: ApplicationInitialized) -> None:
        await self._permissions.restore()
        self._configure_agent(
            self._agents.get(event.metadata.runtime_selection.agent_name)
        )

    async def configure_agent(self, event: AgentConfigured) -> None:
        self._configure_agent(
            self._agents.get(event.runtime_selection.agent_name)
        )

    def _configure_agent(self, agent: AgentDefinition | None) -> None:
        if agent is not None:
            self._permissions.configure_agent(agent.permission_policy)

    async def update_policy(self, event: PolicyChanged) -> None:
        self._permissions.replace_policies(event.permission_policies)


plugin = PermissionsComponent()
