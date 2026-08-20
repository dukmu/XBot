"""Public declarations for the Agent loop and Tool runtime plugins."""

from XBotv2.agentloop.contracts import (
    DEFAULT_MAX_ITERATIONS,
    LoopSettings,
    LoopState,
    ModelRequest,
    ToolRegistration,
)
from XBotv2.agentloop.events import (
    EventContext,
    EventPort,
    Events,
    SHORT_CIRCUIT_EVENTS,
)
from XBotv2.agentloop.services import (
    AgentLoopDriverPort,
    AgentLoopFactoryPort,
    LoopFactoryOptions,
    ToolGuard,
    ToolsPort,
)

__all__ = [
    "AgentLoopDriverPort",
    "AgentLoopEventType",
    "AgentLoopFactoryPort",
    "DEFAULT_MAX_ITERATIONS",
    "EventContext",
    "EventPort",
    "Events",
    "AssistantMessageData",
    "AssistantMessageDeltaData",
    "InputRejectedData",
    "LoopFactoryOptions",
    "LoopSettings",
    "LoopState",
    "ModelRequest",
    "ToolGuard",
    "ToolCallData",
    "ToolCallDeltaData",
    "ToolCallDeltaItemData",
    "ToolCallsStartedData",
    "ToolInfo",
    "ToolListResponse",
    "ToolRegistration",
    "ToolResultData",
    "ToolsPort",
    "TurnCancelledData",
    "TurnData",
    "SHORT_CIRCUIT_EVENTS",
    "agentloop_event",
]

_PROTOCOL_EXPORTS = {
    "AgentLoopEventType",
    "AssistantMessageData",
    "AssistantMessageDeltaData",
    "InputRejectedData",
    "ToolCallData",
    "ToolCallDeltaData",
    "ToolCallDeltaItemData",
    "ToolCallsStartedData",
    "ToolInfo",
    "ToolListResponse",
    "ToolResultData",
    "TurnCancelledData",
    "TurnData",
    "agentloop_event",
}


def __getattr__(name: str) -> object:
    if name not in _PROTOCOL_EXPORTS:
        raise AttributeError(name)
    from XBotv2.agentloop import protocol

    return getattr(protocol, name)
