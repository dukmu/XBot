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
from XBotv2.interactions.interactions import InteractionWaiter


class Events:
    def __init__(self):
        self.items = []

    async def emit(self, name, event):
        self.items.append((name, event))


class Client:
    def __init__(self, response):
        self.response = response

    async def request(self, event, **_kwargs):
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
            "probe", {}, "test", approval=ApprovalService(events, Client(response), InteractionWaiter()),
            apply_permission_decision=handlers.apply_decision,
        )
    assert await store.all() == {}


@pytest.mark.asyncio
async def test_concurrent_grants_survive_restart_without_duplicates(tmp_path):
    events = Events()
    path = tmp_path / "state.json"
    service = PermissionsService({}, RuntimeVariables(), StateService(path=path))
    handlers = PermissionHandlers(service, events.emit)
    approval = ApprovalService(events, Client({"decision": "allow", "scope": "session"}), InteractionWaiter())
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
async def test_pending_approval_retains_its_payload_for_replay(tmp_path):
    """A reconnect during the wait can only rebuild the dialog with the payload.

    The router previously kept only the future/request id, so an unanswered
    approval was unrecoverable once the bounded replay window evicted it.
    """
    from XBotv2.application.client_events import ClientEventRouter
    from XBotv2.core.tools import ClientEvent
    from XBotv2.permissions.protocol import PermissionRequestData

    events = Events()
    waiter = InteractionWaiter()
    router = ClientEventRouter()
    retained_while_waiting: list[list[ClientEvent]] = []

    async def answer_after_probe(event, *, timeout_seconds=None, tool_call_id=""):
        del timeout_seconds, tool_call_id
        request_id = str(event.data["request_id"])
        waiter.register(request_id)
        # A reconnect during the wait can only rebuild the dialog with the
        # payload, not just the request id.
        retained_while_waiting.append(router.pending_interactions())
        result = waiter.answer(request_id, decision="allow", scope="once")
        return {
            "request_id": result.request_id,
            "status": result.status,
            "decision": result.decision,
        }

    router.install(answer_after_probe)
    approval = ApprovalService(events, router, waiter)
    request = ClientEvent(
        type="permission_request",
        data=PermissionRequestData(
            request_id="permission:call-1",
            source="permission_system",
            tool_call=ToolCall(id="call-1", name="shell", args={"command": "pwd"}),
            reason="inspect",
        ).model_dump(exclude_none=True),
    )
    decision = await approval.request(request)

    assert decision.decision == "allow"
    assert [item.type for item in retained_while_waiting[0]] == ["permission_request"]
    assert retained_while_waiting[0][0].data["tool_call"]["id"] == "call-1"
    # Once answered, the snapshot no longer replays it.
    assert router.pending_interactions() == []


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
            approval=ApprovalService(events, Client({"decision": "allow", "scope": "session"}), InteractionWaiter()),
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
