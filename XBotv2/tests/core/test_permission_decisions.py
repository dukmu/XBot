"""Approval responses remain typed at the interaction boundary."""

import asyncio

import pytest

from XBotv2.core import ToolCall
from XBotv2.permissions import Allowed, Denied, PermissionRequest
from XBotv2.permissions.approval import ApprovalService, request_decision
from XBotv2.permissions.contracts import ToolPermission


class _Client:
    def __init__(self, response):
        self.response = response

    async def request(self, _request):
        return self.response


class _Events:
    async def emit(self, *_args):
        return None


class _Waiter:
    def cancel_all(self, _reason):
        return []


def _request() -> PermissionRequest:
    return PermissionRequest(
        interaction_id="permission:call-1",
        source="permission_system",
        subject=ToolPermission(
            tool_call=ToolCall(id="call-1", name="shell", args={"command": "pwd"}),
        ),
        reason="inspect",
        resume_supported=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", [Allowed(scope="once"), Denied(reason="no")])
async def test_approval_service_returns_only_owned_resolution_types(approval):
    service = ApprovalService(_Events(), _Client(approval), _Waiter())
    assert await service.request(_request()) == approval


@pytest.mark.asyncio
async def test_unavailable_approval_fails_closed():
    service = ApprovalService(_Events(), _Client(None), _Waiter())
    assert await service.request(_request()) == Denied(reason="unavailable")


@pytest.mark.asyncio
async def test_wrong_interaction_resolution_is_rejected():
    service = ApprovalService(_Events(), _Client(object()), _Waiter())
    with pytest.raises(TypeError, match="resolution"):
        await service.request(_request())


@pytest.mark.asyncio
async def test_request_decision_applies_exactly_one_received_approval():
    seen = []

    class _Approval:
        async def request(self, request):
            seen.append(request)
            return Allowed(scope="session")

    async def apply(request, approval):
        seen.append(approval)
        return approval

    result = await request_decision(_Approval(), _request(), apply)
    assert result == Allowed(scope="session")
    assert seen == [_request(), result]


@pytest.mark.asyncio
async def test_cancelled_approval_does_not_invent_a_decision():
    class _Cancelled:
        async def request(self, _request):
            raise asyncio.CancelledError

    async def apply(_request, _approval):
        raise AssertionError("no approval was received")

    with pytest.raises(asyncio.CancelledError):
        await request_decision(_Cancelled(), _request(), apply)
