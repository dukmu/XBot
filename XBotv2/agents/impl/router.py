"""Agent C/S wire models and route contribution."""

from __future__ import annotations
from XBotv2.agentloop.api.contracts import AGENT_LOOP

from fastapi import APIRouter
from XBotv2.agents.api.contracts import (
    LIST_AGENTS,
    SELECT_AGENT,
    SelectAgent,
)
from XBotv2.core.api.operations import EmptyRequest
from XBotv2.session.api.contracts import SessionsPort
from XBotv2.agents.api.protocol import (
    AgentInfo, AgentListResponse, AgentSelectionRequest, AgentSelectionResponse,
)

def build_router(*, sessions: SessionsPort) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/agents",
        operation_id="list_agents",
    )
    async def list_agents(session_id: str, thread_id: str) -> AgentListResponse:
        catalog = await (await sessions.locate(session_id, thread_id)).require(AGENT_LOOP).invoke(LIST_AGENTS, EmptyRequest())
        return AgentListResponse(
            active=catalog.active,
            agents=[
                AgentInfo.from_definition(definition)
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
        selected = await (await sessions.locate(session_id, thread_id)).require(AGENT_LOOP).invoke(SELECT_AGENT, SelectAgent(payload.name))
        return AgentSelectionResponse(
            session_id=session_id,
            thread_id=thread_id,
            agent=selected.active,
            provider=selected.provider,
            model=selected.model,
            model_mode=selected.model_mode,
            context_window=selected.context_window,
        )

    return router
