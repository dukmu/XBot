"""Configuration C/S wire models and route contribution."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query
from pydantic import Field, JsonValue, StrictBool, field_validator, model_validator

from XBotv2.config.contracts import (
    GET_POLICY,
    UPDATE_POLICY,
    PatchPolicy,
    PatchPluginConfig,
    PolicySnapshot,
    PluginConfigCatalog,
    PluginConfigConflict,
    PluginConfigScope,
    PluginConfigUnavailable,
    SettingsPort,
)
from XBotv2.core.errors import OperationError
from XBotv2.core.operations import EmptyRequest
from XBotv2.protocol import WireModel
from XBotv2.protocol.http_util import HttpServerError
from XBotv2.session.contracts import SessionsPort


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
    permissions: dict[str, list[dict[str, JsonValue]]] = Field(default_factory=dict)
    effective_permissions: dict[str, list[dict[str, JsonValue]]] = Field(
        default_factory=dict
    )
    sandbox: dict[str, JsonValue] = Field(default_factory=dict)
    effective_sandbox: dict[str, JsonValue] = Field(default_factory=dict)


class PluginConfigPatchRequest(WireModel):
    revision: str = Field(min_length=1)
    config: dict[str, JsonValue] = Field(default_factory=dict)


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


def build_router(*, sessions: SessionsPort, settings: SettingsPort) -> APIRouter:
    router = APIRouter()

    async def _main_thread_id(session_id: str) -> str:
        """Resolve the session's main thread deterministically.

        ``dispatch_all`` returns every active thread (sorted by id), so
        taking its first element silently depended on thread-id ordering
        (e.g. a subagent thread could win). The session-scoped policy route
        addresses the main thread explicitly.
        """
        threads = await sessions.list_threads(session_id)
        main = next(
            (thread.thread_id for thread in threads if thread.kind == "main"),
            "",
        )
        if not main:
            raise OperationError(
                "thread_not_active",
                "Session policy requires an active main thread.",
            )
        return main

    @router.get(
        "/sessions/{session_id}/policy",
        operation_id="get_session_policy",
    )
    async def get_session_policy(session_id: str) -> SessionPolicyResponse:
        thread_id = await _main_thread_id(session_id)
        snapshot = await sessions.dispatch(
            session_id, thread_id, GET_POLICY, EmptyRequest()
        )
        return _policy_response(session_id, snapshot)

    @router.patch(
        "/sessions/{session_id}/policy",
        operation_id="update_session_policy",
    )
    async def update_session_policy(
        session_id: str,
        payload: SessionPolicyPatch,
    ) -> SessionPolicyResponse:
        thread_id = await _main_thread_id(session_id)
        snapshot = await sessions.dispatch(
            session_id,
            thread_id,
            UPDATE_POLICY,
            PatchPolicy(
                permissions=dict(payload.permissions) or None,
                remove_permissions=tuple(payload.remove_permissions),
                sandbox=dict(payload.sandbox) or None,
                remove_sandbox=tuple(payload.remove_sandbox),
            ),
        )
        return _policy_response(session_id, snapshot)

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/plugin-config",
        operation_id="list_plugin_config",
    )
    async def list_plugin_config(
        session_id: str,
        thread_id: str,
        scope: PluginConfigScope = Query(default="workspace"),
    ) -> PluginConfigCatalog:
        thread = await sessions.thread_summary(session_id, thread_id)
        try:
            return settings.plugin_config_catalog(
                thread.workspace_root, scope, session_id
            )
        except ValueError as exc:
            raise HttpServerError("invalid_plugin_config", str(exc), status=400) from exc

    @router.patch(
        "/sessions/{session_id}/threads/{thread_id}/plugin-config/{plugin_id}",
        operation_id="update_plugin_config",
    )
    async def patch_plugin_config(
        session_id: str,
        thread_id: str,
        plugin_id: str,
        payload: PluginConfigPatchRequest,
        scope: PluginConfigScope = Query(default="workspace"),
    ) -> PluginConfigCatalog:
        thread = await sessions.thread_summary(session_id, thread_id)
        try:
            return settings.update_plugin_config(
                thread.workspace_root,
                plugin_id,
                PatchPluginConfig(
                    scope=scope,
                    revision=payload.revision,
                    config=payload.config,
                ),
                session_id,
            )
        except PluginConfigConflict as exc:
            raise HttpServerError("plugin_config_conflict", str(exc), status=409) from exc
        except PluginConfigUnavailable as exc:
            raise HttpServerError("plugin_config_unavailable", str(exc), status=400) from exc
        except ValueError as exc:
            raise HttpServerError("invalid_plugin_config", str(exc), status=400) from exc

    return router


__all__ = [
    "PermissionDecision",
    "SandboxKey",
    "SandboxValue",
    "SessionPolicyPatch",
    "SessionPolicyResponse",
    "PluginConfigPatchRequest",
    "build_router",
]
