"""Command plane routes: discovery and execution of server commands."""

from __future__ import annotations

import shlex
from typing import Literal

from fastapi import APIRouter
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
from XBotv2.session.services import SessionsPort


class CommandRequest(WireModel):
    command: str = ""
    args: list[str] | None = None
    raw: str = ""
    kind: Literal["server", "prompt"] = "server"


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
        include_in_schema=False,
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
        include_in_schema=False,
    )
    async def run_command(
        session_id: str,
        thread_id: str,
        payload: CommandRequest,
    ) -> CommandResponse:
        raw = payload.raw
        command = payload.command.strip().removeprefix("/")
        args = payload.args
        if args is None:
            try:
                parts = shlex.split(raw)
            except ValueError as exc:
                raise HttpServerError(
                    "invalid_request",
                    f"Invalid command syntax: {exc}",
                    status=400,
                ) from exc
            if not command and parts:
                command = parts[0].removeprefix("/")
            args = parts[1:] if parts else []
        if not command:
            raise HttpServerError("invalid_request", "command must be non-empty", status=400)
        catalog = await sessions.dispatch(
            session_id, thread_id, LIST_COMMANDS, EmptyRequest()
        )
        declared = next(
            (item for item in catalog.commands if item.name == command.lower()),
            None,
        )
        raw_args = raw.strip()
        if raw_args.startswith("/"):
            _, _, raw_args = raw_args.partition(" ")
        elif not raw_args:
            raw_args = " ".join(args)
        result = await sessions.dispatch(
            session_id,
            thread_id,
            EXECUTE_COMMAND,
            ExecuteCommand(
                command=command,
                kind=payload.kind,
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
