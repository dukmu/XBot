"""Command HTTP transport models."""

from typing import Literal
from pydantic import Field
from XBotv2.protocol import WireModel
from XBotv2.commands.api.contracts import CommandDescription, CommandExecution


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
