"""Permission guards cannot widen policy decisions."""

import pytest

from XBotv2.agentloop.contracts import ToolRegistration
from XBotv2.core import Tool, ToolCall
from XBotv2.permissions import Allowed, PermissionPolicy, PermissionRule
from XBotv2.permissions.guard import PermissionGuard
from XBotv2.permissions.system import PermissionSystem


def _registration() -> ToolRegistration:
    async def shell(command: str) -> str:
        return command

    tool = Tool.from_function(shell)
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
