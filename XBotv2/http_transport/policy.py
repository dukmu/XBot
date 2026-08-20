"""HTTP adapter for session permission and sandbox policy operations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from XBotv2.config.contracts import (
    GET_POLICY,
    UPDATE_POLICY,
    PatchPolicy,
    PolicySnapshot,
)
from XBotv2.core.operations import EmptyRequest
from XBotv2.protocol.models import SessionPolicyPatch, SessionPolicyResponse
from XBotv2.server.contracts import contribute_router
from XBotv2.session.contracts import dispatch_session_group_operation


def _response(session_id: str, snapshot: PolicySnapshot) -> SessionPolicyResponse:
    return SessionPolicyResponse(
        session_id=session_id,
        permissions=dict(snapshot.policy.get("permissions") or {}),
        effective_permissions=dict(snapshot.effective_permissions),
        sandbox=dict(snapshot.policy.get("sandbox") or {}),
        effective_sandbox=dict(snapshot.effective_sandbox),
    )


def build_router(*, events: Any) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/sessions/{session_id}/policy",
        operation_id="get_session_policy",
    )
    async def get_session_policy(session_id: str) -> SessionPolicyResponse:
        snapshots = await dispatch_session_group_operation(
            events,
            session_id,
            GET_POLICY,
            EmptyRequest(),
        )
        return _response(session_id, snapshots[0])

    @router.patch(
        "/sessions/{session_id}/policy",
        operation_id="update_session_policy",
    )
    async def update_session_policy(
        session_id: str,
        payload: SessionPolicyPatch,
    ) -> SessionPolicyResponse:
        snapshots = await dispatch_session_group_operation(
            events,
            session_id,
            UPDATE_POLICY,
            PatchPolicy(
                permissions=dict(payload.permissions) or None,
                remove_permissions=tuple(payload.remove_permissions),
                sandbox=dict(payload.sandbox) or None,
                remove_sandbox=tuple(payload.remove_sandbox),
            ),
        )
        return _response(session_id, snapshots[0])

    return router


class PolicyHttpAdapter:
    name = "xbot.http.policy"
    inject = ["server"]

    async def apply(self, ctx: Any, config: Any = None) -> None:
        await contribute_router(ctx, owner=self.name, router=build_router(events=ctx))


plugin = PolicyHttpAdapter()
