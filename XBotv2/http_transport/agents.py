"""HTTP adapter for Agent catalog and selection operations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from XBotv2.agents.contracts import (
    LIST_AGENTS,
    RELOAD_AGENTS,
    SELECT_AGENT,
    SelectAgent,
)
from XBotv2.core.operations import EmptyRequest
from XBotv2.server.contracts import contribute_router
from XBotv2.protocol.models import (
    AgentInfo,
    AgentListResponse,
    AgentSelectionRequest,
    AgentSelectionResponse,
)
from XBotv2.session.contracts import SessionRef, dispatch_session_operation


def build_router(*, events: Any) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/agents",
        operation_id="list_agents",
    )
    async def list_agents(session_id: str, thread_id: str) -> AgentListResponse:
        catalog = await dispatch_session_operation(
            events,
            SessionRef(session_id, thread_id),
            LIST_AGENTS,
            EmptyRequest(),
        )
        return AgentListResponse(
            active=catalog.active,
            agents=[
                AgentInfo(
                    name=definition.name,
                    description=definition.description,
                    mode=definition.mode,
                    provider=definition.provider or "",
                    model=definition.model or "",
                    context_window=definition.context_window or 0,
                )
                for definition in catalog.agents
            ],
        )

    @router.put(
        "/sessions/{session_id}/threads/{thread_id}/agent",
        operation_id="select_agent",
    )
    async def select_agent(
        session_id: str,
        thread_id: str,
        payload: AgentSelectionRequest,
    ) -> AgentSelectionResponse:
        selected = await dispatch_session_operation(
            events,
            SessionRef(session_id, thread_id),
            SELECT_AGENT,
            SelectAgent(payload.name),
        )
        return AgentSelectionResponse(
            session_id=session_id,
            thread_id=thread_id,
            agent=selected.active,
            provider=selected.provider,
            model=selected.model,
            model_mode=selected.model_mode,
            context_window=selected.context_window,
        )

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/agents/reload",
        operation_id="reload_agents",
    )
    async def reload_agents(
        session_id: str,
        thread_id: str,
    ) -> AgentListResponse:
        catalog = await dispatch_session_operation(
            events,
            SessionRef(session_id, thread_id),
            RELOAD_AGENTS,
            EmptyRequest(),
        )
        return AgentListResponse(
            active=catalog.active,
            agents=[
                AgentInfo(
                    name=definition.name,
                    description=definition.description,
                    mode=definition.mode,
                    provider=definition.provider or "",
                    model=definition.model or "",
                    context_window=definition.context_window or 0,
                )
                for definition in catalog.agents
            ],
        )

    return router


class AgentsHttpAdapter:
    name = "xbot.http.agents"
    inject = ["server"]

    async def apply(self, ctx: Any, config: Any = None) -> None:
        await contribute_router(ctx, owner=self.name, router=build_router(events=ctx))


plugin = AgentsHttpAdapter()
