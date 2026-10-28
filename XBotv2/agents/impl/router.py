"""Agent C/S wire models and route contribution."""

from __future__ import annotations
from XBotv2.agentloop.api.contracts import AGENT_LOOP

from fastapi import APIRouter, Query
from typing import Literal
from XBotv2.protocol.api.http_util import HttpServerError
from XBotv2.agents.api.contracts import AGENT_CATALOG
from XBotv2.agents.impl.documents import read_document, write_document
from XBotv2.agents.api.contracts import (
    LIST_AGENTS,
    SELECT_AGENT,
    SelectAgent,
)
from XBotv2.core.api.operations import EmptyRequest
from XBotv2.session.api.contracts import SessionsPort
from XBotv2.agents.api.protocol import (
    AgentInfo, AgentListResponse, AgentSelectionRequest, AgentSelectionResponse, AgentDocument, AgentDefinitionNames,
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

    @router.get("/sessions/{session_id}/threads/{thread_id}/agent-definitions", operation_id="list_agent_definitions")
    async def list_agent_definitions(session_id: str, thread_id: str) -> AgentDefinitionNames:
        ctx = await sessions.locate(session_id, thread_id)
        return AgentDefinitionNames(names=[item.name for item in ctx.require(AGENT_CATALOG).definitions()])

    @router.get("/sessions/{session_id}/threads/{thread_id}/agent-definitions/{name}", operation_id="get_agent_definition")
    async def get_agent_document(session_id: str, thread_id: str, name: str, scope: Literal["global", "workspace"] = Query(default="workspace")) -> AgentDocument:
        ctx = await sessions.locate(session_id, thread_id)
        try:
            return AgentDocument.model_validate(read_document(ctx, ctx.require(AGENT_CATALOG), scope, name))
        except ValueError as exc:
            raise HttpServerError("invalid_agent_definition", str(exc), status=400) from exc

    @router.put("/sessions/{session_id}/threads/{thread_id}/agent-definitions/{name}", operation_id="save_agent_definition")
    async def put_agent_document(session_id: str, thread_id: str, name: str, payload: AgentDocument) -> AgentDocument:
        if payload.name != name:
            raise HttpServerError("invalid_agent_definition", "Agent name does not match the request", status=400)
        ctx = await sessions.locate(session_id, thread_id)
        try:
            async with ctx.require(AGENT_LOOP).idle("edit Agent definition"):
                targets = await sessions.scopes(session_id)
                catalogs = [target.require(AGENT_CATALOG) for target in targets]
                definitions = write_document(ctx, payload.scope, name, payload.revision, payload.metadata, payload.prompt, targets=targets)
                for catalog, definition in zip(catalogs, definitions, strict=True):
                    catalog.replace_file_definition(definition, overlay=payload.scope == "workspace")
                return AgentDocument.model_validate(read_document(ctx, ctx.require(AGENT_CATALOG), payload.scope, name))
        except FileExistsError as exc:
            raise HttpServerError("agent_definition_conflict", str(exc), status=409) from exc
        except ValueError as exc:
            raise HttpServerError("invalid_agent_definition", str(exc), status=400) from exc

    return router
