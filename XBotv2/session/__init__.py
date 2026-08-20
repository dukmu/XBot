"""Public declarations for the session identity and runtime-host plugin."""

from XBotv2.session.commands import build_session_commands
from XBotv2.session.contracts import (
    AgentApplicationFactory,
    AgentApplicationOptions,
    DISPATCH_OPERATION,
    DISPATCH_SESSION_OPERATION,
    OPERATION_COMPLETED,
    PREPARE_FORK,
    PrepareFork,
    SessionDispatch,
    SessionGroupDispatch,
    SessionOperationCompleted,
    SessionRef,
    SessionStatus,
    dispatch_session_group_operation,
    dispatch_session_operation,
)
from XBotv2.session.services import SessionPort

__all__ = [
    "AgentApplicationFactory",
    "AgentApplicationOptions",
    "DISPATCH_OPERATION",
    "DISPATCH_SESSION_OPERATION",
    "OPERATION_COMPLETED",
    "PREPARE_FORK",
    "PrepareFork",
    "SessionDispatch",
    "SessionGroupDispatch",
    "SessionOperationCompleted",
    "SessionPort",
    "SessionRef",
    "SessionStatus",
    "build_session_commands",
    "dispatch_session_group_operation",
    "dispatch_session_operation",
]
