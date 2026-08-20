"""Public declarations for the session identity and runtime-host plugin."""

from XBotv2.session.types import (
    AttachmentUpload,
    HistoryMutation,
    ImageUpload,
    InteractionReceipt,
    InterruptResult,
    OpenedSession,
    OpenSession,
    OpenThread,
    SendMessage,
    SessionExists,
    SessionInfo,
    SessionNotFound,
    SessionStreamEvent,
    SessionSnapshot,
    ThreadNotActive,
    ThreadSnapshot,
)

__all__ = [
    "AgentApplicationFactory",
    "AgentApplicationOptions",
    "AgentConfiguredData",
    "AttachmentInput",
    "DISPATCH_OPERATION",
    "DISPATCH_SESSION_OPERATION",
    "CloseResponse",
    "CompletionNoticeData",
    "ForkResponse",
    "HistoryMutationResponse",
    "HistoryUpdatedData",
    "ImageInput",
    "InterruptResponse",
    "MessageData",
    "MessageRequest",
    "OPERATION_COMPLETED",
    "PREPARE_FORK",
    "PrepareFork",
    "OpenSessionRequest",
    "OpenSessionResponse",
    "OpenThreadRequest",
    "SessionDispatch",
    "SessionGroupDispatch",
    "SessionOperationCompleted",
    "SessionsPort",
    "SessionHistoryItem",
    "SessionListResponse",
    "SessionEventType",
    "SessionMode",
    "SessionSummary",
    "SessionPort",
    "SessionRef",
    "SessionStatus",
    "AttachmentUpload",
    "HistoryMutation",
    "ImageUpload",
    "InteractionReceipt",
    "InterruptResult",
    "OpenedSession",
    "OpenSession",
    "OpenThread",
    "SendMessage",
    "SessionExists",
    "SessionInfo",
    "SessionNotFound",
    "SessionStreamEvent",
    "SessionSnapshot",
    "ThreadNotActive",
    "ThreadListResponse",
    "ThreadMessagesResponse",
    "ThreadSummary",
    "ThreadSnapshot",
    "UndoRequest",
    "build_session_commands",
    "dispatch_session_group_operation",
    "dispatch_session_operation",
    "session_event",
    "session_error_event",
]

_COMMAND_EXPORTS = {"build_session_commands"}
_CONTRACT_EXPORTS = {
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
    "SessionRef",
    "SessionStatus",
    "dispatch_session_group_operation",
    "dispatch_session_operation",
}
_SERVICE_EXPORTS = {"SessionPort", "SessionsPort"}

_PROTOCOL_EXPORTS = {
    "AgentConfiguredData",
    "AttachmentInput",
    "CloseResponse",
    "CompletionNoticeData",
    "ForkResponse",
    "HistoryMutationResponse",
    "HistoryUpdatedData",
    "ImageInput",
    "InterruptResponse",
    "MessageData",
    "MessageRequest",
    "OpenSessionRequest",
    "OpenSessionResponse",
    "OpenThreadRequest",
    "SessionEventType",
    "SessionHistoryItem",
    "SessionListResponse",
    "SessionMode",
    "SessionSummary",
    "ThreadListResponse",
    "ThreadMessagesResponse",
    "ThreadSummary",
    "UndoRequest",
    "session_error_event",
    "session_event",
}


def __getattr__(name: str) -> object:
    if name in _COMMAND_EXPORTS:
        from XBotv2.session import commands as declarations
    elif name in _CONTRACT_EXPORTS:
        from XBotv2.session import contracts as declarations
    elif name in _SERVICE_EXPORTS:
        from XBotv2.session import services as declarations
    elif name in _PROTOCOL_EXPORTS:
        from XBotv2.session import protocol as declarations
    else:
        raise AttributeError(name)

    return getattr(declarations, name)
