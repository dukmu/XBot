"""Permission guards cannot widen policy decisions."""

from dataclasses import replace

import pytest

from XBotv2.agentloop.contracts import ToolRegistration
from XBotv2.core import SandboxEscape, Tool, ToolCall
from XBotv2.permissions import Allowed, PermissionPolicy, PermissionRule
from XBotv2.permissions.guard import PermissionGuard
from XBotv2.permissions.system import PermissionSystem


def _registration() -> ToolRegistration:
    async def shell(command: str) -> str:
        return command

    tool = Tool.from_function(shell)
    return ToolRegistration(tool=tool, registered_name=tool.name)


def _escaping_registration() -> ToolRegistration:
    async def executor(command: str, privilege: str = "sandboxed") -> str:
        return command

    tool = Tool.from_function(executor)
    tool = replace(
        tool,
        sandbox_escape=lambda args: (
            SandboxEscape(
                argument="privilege",
                reason="Execution outside the sandbox requires approval.",
            )
            if args.get("privilege") == "outside"
            else None
        ),
    )
    return ToolRegistration(tool=tool, registered_name=tool.name)


class _Approval:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def request(self, _request):
        self.calls += 1
        return self.result


async def _identity(_request, approval):
    return approval


async def _emit(*_args):
    return None


@pytest.mark.asyncio
async def test_policy_deny_never_requests_approval():
    permissions = PermissionSystem(PermissionPolicy(rules=(
        PermissionRule(tool_pattern="shell", decision="deny"),
    )))
    approval = _Approval(Allowed(scope="session"))
    guard = PermissionGuard(permissions, approval, _emit, _identity)

    result = await guard.check(
        ToolCall(id="call-1", name="shell", args={"command": "pwd"}),
        _registration(),
    )
    assert result is not None and result.action == "deny"
    assert approval.calls == 0


@pytest.mark.asyncio
async def test_policy_change_to_deny_while_waiting_prevents_execution():
    permissions = PermissionSystem(PermissionPolicy(default_decision="ask"))

    class _ChangingApproval:
        async def request(self, _request):
            permissions.replace_policies((PermissionPolicy(rules=(
                PermissionRule(tool_pattern="shell", decision="deny"),
            )),))
            return Allowed(scope="session")

    guard = PermissionGuard(permissions, _ChangingApproval(), _emit, _identity)
    result = await guard.check(
        ToolCall(id="call-1", name="shell", args={"command": "pwd"}),
        _registration(),
    )
    assert result is not None and result.action == "deny"


@pytest.mark.asyncio
async def test_explicit_allow_passes_without_approval():
    permissions = PermissionSystem(PermissionPolicy(rules=(
        PermissionRule(tool_pattern="shell", decision="allow"),
    )))
    approval = _Approval(Allowed(scope="once"))
    guard = PermissionGuard(permissions, approval, _emit, _identity)
    assert await guard.check(
        ToolCall(id="call-1", name="shell", args={"command": "pwd"}),
        _registration(),
    ) is None
    assert approval.calls == 0


@pytest.mark.asyncio
async def test_tool_declared_sandbox_escape_requires_approval_despite_blanket_allow():
    permissions = PermissionSystem(PermissionPolicy(rules=(
        PermissionRule(tool_pattern="executor", decision="allow"),
    )))
    approval = _Approval(Allowed(scope="once"))
    guard = PermissionGuard(permissions, approval, _emit, _identity)

    assert await guard.check(
        ToolCall(
            id="call-escape",
            name="executor",
            args={"command": "pwd", "privilege": "outside"},
        ),
        _escaping_registration(),
    ) is None
    assert approval.calls == 1


@pytest.mark.asyncio
async def test_non_escaping_call_uses_the_normal_permission_decision():
    permissions = PermissionSystem(PermissionPolicy(rules=(
        PermissionRule(tool_pattern="executor", decision="allow"),
    )))
    approval = _Approval(Allowed(scope="once"))
    guard = PermissionGuard(permissions, approval, _emit, _identity)

    assert await guard.check(
        ToolCall(
            id="call-sandboxed",
            name="executor",
            args={"command": "pwd", "privilege": "sandboxed"},
        ),
        _escaping_registration(),
    ) is None
    assert approval.calls == 0
