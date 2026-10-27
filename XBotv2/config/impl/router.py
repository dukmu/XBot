"""Configuration C/S wire models and route contribution."""

from __future__ import annotations

from fastapi import APIRouter, Query
from XBotv2.config.api.contracts import (
    GET_POLICY,
    UPDATE_POLICY,
    PatchPolicy,
    PatchPluginConfig,
    PolicySnapshot,
    PluginConfigCatalog,
    PluginConfigConflict,
    PluginConfigScope,
    PluginConfigUnavailable,
)
from XBotv2.core.api.errors import OperationError
from XBotv2.core.api.operations import EmptyRequest
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.config.impl.plugin_catalog import plugin_config_catalog, update_plugin_config
from XBotv2.protocol.api.http_util import HttpServerError
from XBotv2.permissions.api.models import PermissionPolicy
from XBotv2.sandbox.api.models import SandboxConfig
from XBotv2.session.contracts import SessionsPort
from XBotv2.config.api.protocol import (
    PluginConfigPatchRequest, SessionPolicyPatch, SessionPolicyResponse,
)

def _policy_response(
    session_id: str,
    snapshot: PolicySnapshot,
) -> SessionPolicyResponse:
    return SessionPolicyResponse(
        session_id=session_id,
        permissions=PermissionPolicy.model_validate(
            snapshot.policy.get("permissions") or {}
        ),
        effective_permissions=PermissionPolicy.model_validate(
            snapshot.effective_permissions
        ),
        sandbox=SandboxConfig.model_validate(snapshot.policy.get("sandbox") or {}),
        effective_sandbox=SandboxConfig.model_validate(snapshot.effective_sandbox),
    )


def build_router(*, sessions: SessionsPort, paths: RuntimePaths) -> APIRouter:
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
            return plugin_config_catalog(
                paths, thread.workspace_root, scope, session_id
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
            return update_plugin_config(
                paths,
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
