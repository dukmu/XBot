"""Session-facing HTTP helpers for the server route modules.

These helpers bridge HTTP handlers to the session application: they resolve
the owning session runtime and live plugin services. They are session
capability business, not wire protocol.
"""

from __future__ import annotations

from typing import Any

from XBotv2.protocol.models import (
    InteractionResponse,
    OpenSessionResponse,
)
from XBotv2.core.history import display_history
from XBotv2.protocol.http_util import HttpServerError
from XBotv2.session.manager import (
    SessionManager,
    pending_interactions,
)


async def _open_session_response(ctx: Any) -> OpenSessionResponse:
    loader = ctx.services.get("loader")
    status_slots = await loader.status_slots() if loader is not None else {}
    return OpenSessionResponse(
        session_id=ctx.session_id,
        thread_id=ctx.thread_id,
        agent_name=ctx.engine.settings.agent_name,
        workspace_root=ctx.workspace_root,
        provider=ctx.provider_name,
        model=ctx.engine.settings.model,
        model_mode=ctx.engine.settings.model_mode,
        context_window=ctx.engine.settings.context_window,
        usage=ctx.services.usage.snapshot(),
        history=display_history(ctx.engine.messages),
        status_slots=status_slots,
    )


async def _resolve_interaction(
    *,
    manager: SessionManager,
    session_id: str,
    thread_id: str,
    payload: dict[str, Any],
    kind: str,
) -> InteractionResponse:
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        raise HttpServerError(
            "invalid_request",
            f"{kind}.response payload.request_id must be non-empty",
            status=400,
        )
    ctx = await manager.get(session_id, thread_id)

    if kind == "permission":
        decision = str(payload.get("decision") or "").strip().lower()
        if decision not in {"allow", "deny"}:
            raise HttpServerError(
                "invalid_request",
                "permission.response payload.decision must be allow or deny",
                status=400,
            )
        scope = str(payload.get("scope") or "once").strip().lower()
        if scope not in {"once", "session"}:
            raise HttpServerError(
                "invalid_request",
                "permission.response payload.scope must be once or session",
                status=400,
            )
        try:
            approval = _plugin_service(ctx, "approval")
            if approval is None or not hasattr(approval, "submit"):
                raise RuntimeError("Required approval service is unavailable")
            approval.submit(request_id, decision, scope)
        except Exception as exc:  # noqa: BLE001
            raise HttpServerError(
                "interaction_no_longer_pending",
                str(exc),
                status=410,
            ) from exc
        return InteractionResponse(
            request_id=request_id,
            pending_interactions=pending_interactions(ctx),
        )

    answer = payload.get("answer", "")
    try:
        interactions = _plugin_service(ctx, "interactions")
        if interactions is None or not hasattr(interactions, "submit_user_input"):
            raise RuntimeError("Required interactions service is unavailable")
        interactions.submit_user_input(request_id, answer)
    except Exception as exc:  # noqa: BLE001
        raise HttpServerError(
            "interaction_no_longer_pending",
            str(exc),
            status=410,
        ) from exc
    return InteractionResponse(
        request_id=request_id,
        pending_interactions=pending_interactions(ctx),
    )


def _plugin_service(ctx: Any, name: str) -> Any:
    """Resolve one plugin service from the owning session application."""
    services = getattr(ctx, "services", None)
    if services is None or not hasattr(services, "get"):
        return None
    return services.get(name)