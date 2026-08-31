"""Agent C/S wire models and route contribution."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import Field

from XBotv2.agents.contracts import (
    LIST_AGENTS,
    SELECT_AGENT,
    SelectAgent,
)
from XBotv2.core.operations import EmptyRequest
from XBotv2.protocol import WireModel
from XBotv2.session import SessionsPort


class AgentInfo(WireModel):
    name: str = Field(min_length=1)
    description: str
    mode: Literal["primary", "subagent", "all"]
    provider: str = ""
    model: str = ""
    context_window: int = Field(default=0, ge=0)


class AgentListResponse(WireModel):
    active: str = ""
    agents: list[AgentInfo] = Field(default_factory=list)


class AgentSelectionRequest(WireModel):
    name: str = Field(min_length=1)


class AgentSelectionResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str
    model_mode: str = ""
    context_window: int = Field(ge=0)


def build_router(*, sessions: SessionsPort) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/agents",
        operation_id="list_agents",
    )
    async def list_agents(session_id: str, thread_id: str) -> AgentListResponse:
        catalog = await sessions.dispatch(
            session_id, thread_id, LIST_AGENTS, EmptyRequest()
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
        selected = await sessions.dispatch(
            session_id, thread_id, SELECT_AGENT, SelectAgent(payload.name)
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

    return router


__all__ = [
    "AgentInfo",
    "AgentListResponse",
    "AgentSelectionRequest",
    "AgentSelectionResponse",
    "build_router",
]
