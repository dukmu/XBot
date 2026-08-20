"""Configuration C/S wire models and route contribution."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import Field, StrictBool, field_validator, model_validator

from XBotv2.config.contracts import (
    GET_POLICY,
    UPDATE_POLICY,
    PatchPolicy,
    PolicySnapshot,
)
from XBotv2.loader import RELOAD_PLUGINS
from XBotv2.core.operations import EmptyRequest
from XBotv2.protocol import WireModel
from XBotv2.server import contribute_router
from XBotv2.session import (
    SessionRef,
    dispatch_session_group_operation,
    dispatch_session_operation,
)


PermissionDecision = Literal["allow", "deny", "ask"]
SandboxAccess = Literal["allow", "deny", "readonly", "readwrite"]
SandboxKey = Literal[
    "enabled",
    "network",
    "external_read",
    "external_write",
    "workspace_read",
    "workspace_write",
]
SandboxValue = StrictBool | SandboxAccess


class SessionPolicyPatch(WireModel):
    permissions: dict[str, PermissionDecision] = Field(default_factory=dict)
    remove_permissions: list[str] = Field(default_factory=list)
    sandbox: dict[SandboxKey, SandboxValue] = Field(default_factory=dict)
    remove_sandbox: list[SandboxKey] = Field(default_factory=list)

    @field_validator("permissions")
    @classmethod
    def _validate_permission_names(
        cls, value: dict[str, PermissionDecision]
    ) -> dict[str, PermissionDecision]:
        if any(not name.strip() for name in value):
            raise ValueError("permission tool names must be non-empty")
        return {name.strip(): decision for name, decision in value.items()}

    @field_validator("remove_permissions")
    @classmethod
    def _validate_removed_permission_names(cls, value: list[str]) -> list[str]:
        if any(not name.strip() for name in value):
            raise ValueError("permission tool names must be non-empty")
        return [name.strip() for name in value]

    @model_validator(mode="after")
    def _validate_policy_patch(self) -> "SessionPolicyPatch":
        permission_overlap = set(self.permissions).intersection(
            self.remove_permissions
        )
        sandbox_overlap = set(self.sandbox).intersection(self.remove_sandbox)
        if permission_overlap or sandbox_overlap:
            raise ValueError("policy keys cannot be set and removed together")
        for key, value in self.sandbox.items():
            if key in {"enabled", "network"} and not isinstance(value, bool):
                raise ValueError(f"sandbox.{key} must be a boolean")
            if key not in {"enabled", "network"} and isinstance(value, bool):
                raise ValueError(f"sandbox.{key} must be an access mode")
        return self


class SessionPolicyResponse(WireModel):
    session_id: str = Field(min_length=1)
    permissions: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    effective_permissions: dict[str, list[dict[str, Any]]] = Field(
        default_factory=dict
    )
    sandbox: dict[str, Any] = Field(default_factory=dict)
    effective_sandbox: dict[str, Any] = Field(default_factory=dict)


class ConfigReloadResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    reloaded: list[str] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    model_mode: str = ""
    context_window: int = Field(default=0, ge=0)
    errors: list[str] = Field(default_factory=list)


def _policy_response(
    session_id: str,
    snapshot: PolicySnapshot,
) -> SessionPolicyResponse:
    return SessionPolicyResponse(
        session_id=session_id,
        permissions=dict(snapshot.policy.get("permissions") or {}),
        effective_permissions=dict(snapshot.effective_permissions),
        sandbox=dict(snapshot.policy.get("sandbox") or {}),
        effective_sandbox=dict(snapshot.effective_sandbox),
    )


def build_router(*, events: Any) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/config/reload",
        operation_id="reload_config",
    )
    async def reload_config(
        session_id: str,
        thread_id: str,
    ) -> ConfigReloadResponse:
        reloaded = await dispatch_session_operation(
            events,
            SessionRef(session_id, thread_id),
            RELOAD_PLUGINS,
            EmptyRequest(),
        )
        return ConfigReloadResponse(
            session_id=session_id,
            thread_id=thread_id,
            reloaded=list(reloaded.reloaded),
            provider=reloaded.provider,
            model=reloaded.model,
            model_mode=reloaded.model_mode,
            context_window=reloaded.context_window,
            errors=list(reloaded.errors),
        )

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
        return _policy_response(session_id, snapshots[0])

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
        return _policy_response(session_id, snapshots[0])

    return router


class ConfigProtocolPlugin:
    name = "xbot.protocol.config"
    inject = ["server"]

    async def apply(self, ctx: Any, config: Any = None) -> None:
        await contribute_router(ctx, owner=self.name, router=build_router(events=ctx))


plugin = ConfigProtocolPlugin()


__all__ = [
    "ConfigReloadResponse",
    "PermissionDecision",
    "SandboxKey",
    "SandboxValue",
    "SessionPolicyPatch",
    "SessionPolicyResponse",
    "build_router",
]
