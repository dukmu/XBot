"""Command plane routes: discovery and execution of server commands."""

from __future__ import annotations

import shlex
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import Field
from XBotv2.protocol.http_util import HttpServerError
from XBotv2.protocol import WireModel
from XBotv2.server import contribute_router
from XBotv2.commands.contracts import (
    EXECUTE_COMMAND,
    ExecuteCommand,
    LIST_COMMANDS,
)
from XBotv2.core.operations import EmptyRequest
from XBotv2.session import SessionRef, dispatch_session_operation


class CommandRequest(WireModel):
    command: str = ""
    args: list[str] | None = None
    raw: str = ""
    kind: Literal["server", "prompt"] = "server"


class CommandInfo(WireModel):
    name: str
    slash: str
    kind: Literal["client", "server", "prompt"]
    description: str
    usage: str = ""
    examples: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)


class CommandListResponse(WireModel):
    commands: list[CommandInfo]


class CommandResult(WireModel):
    command: str
    status: Literal["ok", "error"]
    message: str


class CommandResponse(WireModel):
    type: Literal["command_result"] = "command_result"
    data: CommandResult


def build_commands_router(*, events: Any) -> APIRouter:
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
        catalog = await dispatch_session_operation(
            events,
            SessionRef(session_id, thread_id),
            LIST_COMMANDS,
            EmptyRequest(),
        )
        return CommandListResponse(
            commands=[{
                "name": command.name,
                "slash": command.slash,
                "kind": command.kind,
                "description": command.description,
                "usage": command.usage,
                "examples": list(command.examples),
                "parameters": command.parameters,
            } for command in catalog.commands]
        )

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
        raw_args = raw.strip()
        if raw_args.startswith("/"):
            _, _, raw_args = raw_args.partition(" ")
        elif not raw_args:
            raw_args = " ".join(args)
        result = await dispatch_session_operation(
            events,
            SessionRef(session_id, thread_id),
            EXECUTE_COMMAND,
            ExecuteCommand(
                command=command,
                kind=payload.kind,
                raw_args=raw_args,
            ),
        )
        return CommandResponse.model_validate({
            "type": "command_result",
            "data": {
                "command": result.command,
                "status": result.status,
                "message": result.message,
            },
        })

    return router


class CommandsProtocolPlugin:
    """Contribute command-plane routes through the XCore route event."""

    inject = ["server"]
    name = "xbot.protocol.commands"

    async def apply(self, ctx: Any, config: Any = None) -> None:
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_commands_router(events=ctx),
        )


plugin = CommandsProtocolPlugin()


__all__ = [
    "CommandInfo",
    "CommandListResponse",
    "CommandRequest",
    "CommandResponse",
    "CommandResult",
    "build_commands_router",
]
