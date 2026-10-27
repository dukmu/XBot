"""Command HTTP routes."""

from fastapi import APIRouter
from XBotv2.protocol.api.http_util import HttpServerError
from XBotv2.commands.api.contracts import EXECUTE_COMMAND, ExecuteCommand, LIST_COMMANDS
from XBotv2.commands.api.protocol import CommandListResponse, CommandRequest, CommandResponse
from XBotv2.core.api.operations import EmptyRequest
from XBotv2.session.contracts import SessionsPort


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
