"""Public declarations for session identity and runtime management."""

from XBotv2.session.types import (
    AttachmentUpload,
    ArtifactPayload,
    HistoryMutation,
    ImageUpload,
    InteractionReceipt,
    InterruptResult,
    MessagePage,
    OpenedSession,
    OpenSession,
    OpenThread,
    PendingInputSnapshot,
    PendingInputUpdate,
    RegenerateMessage,
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
    "ArtifactPayload",
    "CloseResponse",
    "DeleteSessionResponse",
    "ForkResponse",
    "HISTORY_CHANGED",
    "HistoryChanged",
    "HistoryMutationResponse",
    "HistoryUpdatedData",
    "ImageInput",
    "InterruptResponse",
    "MessageData",
    "MessageRequest",
    "MessagePage",
    "PREPARE_FORK",
    "PrepareFork",
    "OpenSessionRequest",
    "OpenSessionResponse",
    "OpenThreadRequest",
    "PendingInputData",
    "PendingInputListResponse",
    "PendingInputUpdateRequest",
    "RegenerateMessage",
    "RegenerateRequest",
    "SessionsPort",
    "SessionHistoryItem",
    "SessionListResponse",
    "SessionEventType",
    "SessionMode",
    "SessionSummary",
    "SessionPort",
    "SessionStatus",
    "AttachmentUpload",
    "HistoryMutation",
    "ImageUpload",
    "InteractionReceipt",
    "InterruptResult",
    "OpenedSession",
    "OpenSession",
    "OpenThread",
    "PendingInputSnapshot",
    "PendingInputUpdate",
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
    "session_event",
    "session_error_event",
]

_COMMAND_EXPORTS = {"build_session_commands"}
_CONTRACT_EXPORTS = {
    "AgentApplicationFactory",
    "AgentApplicationOptions",
    "HISTORY_CHANGED",
    "HistoryChanged",
    "PREPARE_FORK",
    "PrepareFork",
    "SessionStatus",
}
_SERVICE_EXPORTS = {"SessionPort", "SessionsPort"}

_PROTOCOL_EXPORTS = {
    "AgentConfiguredData",
    "AttachmentInput",
    "CloseResponse",
    "DeleteSessionResponse",
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
    "PendingInputData",
    "PendingInputListResponse",
    "PendingInputUpdateRequest",
    "RegenerateRequest",
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
