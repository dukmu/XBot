"""Approval boundaries: durability, concurrency and current-policy decisions."""

import asyncio

import pytest
from xcore.state import StateService

from XBotv2.core import ToolCall
from XBotv2.core.variables import RuntimeVariables
from XBotv2.permissions import ApprovalDecision
from XBotv2.permissions.approval import ApprovalService
from XBotv2.permissions.guard import PermissionGuard
from XBotv2.permissions.plugin import PermissionHandlers, PermissionsService
from XBotv2.permissions.tools import request_tool_permission


class Events:
    def __init__(self):
        self.items = []

    async def emit(self, name, event):
        self.items.append((name, event))


class Client:
    def __init__(self, response):
        self.response = response

    async def request(self, event):
        return self.response


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"decision": "allow", "scope": "global"},
    {"decision": "invalid"},
])
async def test_real_approval_boundary_rejects_invalid_responses(tmp_path, response):
    events = Events()
    store = StateService(path=tmp_path / "state.json")
    service = PermissionsService({}, RuntimeVariables(), store)
    handlers = PermissionHandlers(service, events.emit)
    with pytest.raises(ValueError):
        await request_tool_permission(
            "probe", {}, "test", approval=ApprovalService(events, Client(response)),
            apply_permission_decision=handlers.apply_decision,
        )
    assert await store.all() == {}


@pytest.mark.asyncio
async def test_concurrent_grants_survive_restart_without_duplicates(tmp_path):
    events = Events()
    path = tmp_path / "state.json"
    service = PermissionsService({}, RuntimeVariables(), StateService(path=path))
    handlers = PermissionHandlers(service, events.emit)
    approval = ApprovalService(events, Client({"decision": "allow", "scope": "session"}))
    await asyncio.gather(*(
        request_tool_permission(
            tool, {}, "test", approval=approval,
            apply_permission_decision=handlers.apply_decision,
        ) for tool in ("first", "second", "first")
    ))
    store = StateService(path=path)
    restored = PermissionsService({}, RuntimeVariables(), store)
    await restored.restore()
    assert len(await store.get("grants")) == 2
    assert restored.check("first") == restored.check("second") == "allow"


@pytest.mark.asyncio
async def test_policy_deny_added_while_waiting_prevents_execution_and_grant(tmp_path):
    events = Events()
    store = StateService(path=tmp_path / "state.json")
    service = PermissionsService({}, RuntimeVariables(), store)
    handlers = PermissionHandlers(service, events.emit)

    class ChangedPolicy:
        async def request(self, event):
            service.replace_rules({"deny": [{"tool": "shell"}]})
            return ApprovalDecision(decision="allow", scope="session")

    result = await PermissionGuard(service, ChangedPolicy(), events.emit, handlers.apply_decision).check(
        ToolCall(id="pending", name="shell", args={"command": "pwd"}), None,
    )
    assert result.action == "deny"
    assert await store.all() == {}
    assert events.items[-1][1].decision == "deny"


@pytest.mark.asyncio
async def test_failed_grant_write_does_not_authorize_or_publish_success(tmp_path, caplog):
    class BrokenStore(StateService):
        async def set(self, key, value):
            raise OSError("write failed")

    events = Events()
    service = PermissionsService({}, RuntimeVariables(), BrokenStore(path=tmp_path / "state.json"))
    handlers = PermissionHandlers(service, events.emit)
    caplog.set_level("INFO", logger="xbotv2.permissions")
    with pytest.raises(OSError, match="write failed"):
        await request_tool_permission(
            "probe", {}, "test",
            approval=ApprovalService(events, Client({"decision": "allow", "scope": "session"})),
            apply_permission_decision=handlers.apply_decision,
        )
    assert service.check("probe") == "ask"
    assert not any(name == "permissions/decided" for name, _event in events.items)
    assert caplog.text.count("permission.finished") == 1
    assert 'outcome="error"' in caplog.text


@pytest.mark.asyncio
async def test_cancelled_approval_does_not_call_decision_handler():
    class Cancelled:
        async def request(self, event):
            raise asyncio.CancelledError

    async def unexpected_apply(event, decision):
        raise AssertionError("No decision was received")

    with pytest.raises(asyncio.CancelledError):
        await request_tool_permission(
            "probe", {}, "test", approval=Cancelled(),
            apply_permission_decision=unexpected_apply,
        )
