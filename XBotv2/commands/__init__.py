"""Public declarations for the command-plane plugin."""

from XBotv2.commands.contracts import (
    EXECUTE_COMMAND,
    LIST_COMMANDS,
    CommandCatalog,
    ExecuteCommand,
)
__all__ = [
    "CommandCatalog",
    "CommandInfo",
    "CommandListResponse",
    "CommandRequest",
    "CommandResponse",
    "CommandResult",
    "EXECUTE_COMMAND",
    "ExecuteCommand",
    "LIST_COMMANDS",
]

_PROTOCOL_EXPORTS = {
    "CommandInfo",
    "CommandListResponse",
    "CommandRequest",
    "CommandResponse",
    "CommandResult",
}


def __getattr__(name: str) -> object:
    if name not in _PROTOCOL_EXPORTS:
        raise AttributeError(name)
    from XBotv2.commands import protocol

    return getattr(protocol, name)
