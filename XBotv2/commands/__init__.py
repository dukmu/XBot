"""Public declarations for the command-plane plugin."""

from XBotv2.commands.contracts import (
    EXECUTE_COMMAND,
    LIST_COMMANDS,
    Command,
    CommandCatalog,
    CommandDescription,
    CommandExecution,
    CommandHandler,
    CommandResult,
    ExecuteCommand,
    command_error,
    command_usage,
    guard_command,
    split_command_args,
)
__all__ = [
    "Command",
    "CommandCatalog",
    "CommandDescription",
    "CommandExecution",
    "CommandHandler",
    "CommandInfo",
    "CommandListResponse",
    "CommandRequest",
    "CommandResponse",
    "CommandResult",
    "EXECUTE_COMMAND",
    "ExecuteCommand",
    "LIST_COMMANDS",
    "command_error",
    "command_usage",
    "guard_command",
    "split_command_args",
]

_PROTOCOL_EXPORTS = {
    "CommandInfo",
    "CommandListResponse",
    "CommandRequest",
    "CommandResponse",
}


def __getattr__(name: str) -> object:
    if name not in _PROTOCOL_EXPORTS:
        raise AttributeError(name)
    from XBotv2.commands import protocol

    return getattr(protocol, name)
