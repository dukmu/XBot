"""Command plane routes: discovery and execution of server commands."""

from __future__ import annotations

import shlex
from typing import Literal

from fastapi import APIRouter
from pydantic import Field
from XBotv2.protocol.http_util import HttpServerError
from XBotv2.protocol import WireModel
from XBotv2.commands.contracts import (
    CommandDescription,
    CommandExecution,
    EXECUTE_COMMAND,
    ExecuteCommand,
    LIST_COMMANDS,
)
from XBotv2.core.operations import EmptyRequest
from XBotv2.session.contracts import SessionsPort


class CommandRequest(WireModel):
    """One line, exactly as the user typed it.

    The client does not split it, and does not say what kind of command it is:
    the server owns the catalogue, so the server is what resolves the line. Any
    other split is a second implementation of the same rule, in every client.
    """

    raw: str = Field(min_length=1)


class CommandListResponse(WireModel):
    commands: list[CommandDescription]


class CommandResponse(WireModel):
    type: Literal["command_result"] = "command_result"
    data: CommandExecution


def build_commands_router(*, sessions: SessionsPort) -> APIRouter:
    """Command discovery and execution routes (the command plane)."""

    router = APIRouter()

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/commands",
        operation_id="list_commands",
    )
    async def session_commands(
        session_id: str,
        thread_id: str,
    ) -> CommandListResponse:
        catalog = await sessions.dispatch(
            session_id, thread_id, LIST_COMMANDS, EmptyRequest()
        )
        return CommandListResponse(commands=list(catalog.commands))

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/commands",
        operation_id="run_command",
    )
    async def run_command(
        session_id: str,
        thread_id: str,
        payload: CommandRequest,
    ) -> CommandResponse:
        command = payload.raw.strip().removeprefix("/").split(" ", 1)[0].lower()
        if not command:
            raise HttpServerError("invalid_request", "command must be non-empty", status=400)
        raw_args = payload.raw.strip()
        if raw_args.startswith("/"):
            _, _, raw_args = raw_args.partition(" ")
        catalog = await sessions.dispatch(
            session_id, thread_id, LIST_COMMANDS, EmptyRequest()
        )
        declared = next(
            (item for item in catalog.commands if item.name == command),
            None,
        )
        result = await sessions.dispatch(
            session_id,
            thread_id,
            EXECUTE_COMMAND,
            ExecuteCommand(
                command=command,
                kind=declared.kind if declared is not None else "server",
                raw_args=raw_args,
                exclusive=declared.exclusive if declared is not None else True,
            ),
        )
        return CommandResponse(data=result)

    return router


__all__ = [
    "CommandListResponse",
    "CommandRequest",
    "CommandResponse",
    "build_commands_router",
]
