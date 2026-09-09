"""Public declarations for live client interactions."""

from XBotv2.interactions.contracts import (
    InteractionNotPending,
    InteractionResult,
    InteractionWaiterPort,
    InteractionsPort,
)
from XBotv2.interactions.protocol import (
    ClientMessageData,
    InteractionRecordedData,
    InteractionEventType,
    InteractionResponse,
    UserInputOption,
    UserInputRequiredData,
    UserInputResponseRequest,
    interaction_recorded_event,
)

__all__ = [
    "ClientMessageData",
    "InteractionRecordedData",
    "InteractionEventType",
    "InteractionResponse",
    "InteractionNotPending",
    "InteractionResult",
    "InteractionWaiterPort",
    "InteractionsPort",
    "UserInputOption",
    "UserInputRequiredData",
    "UserInputResponseRequest",
    "interaction_recorded_event",
]
