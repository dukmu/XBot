"""Public declarations for live client interactions."""

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
    "UserInputOption",
    "UserInputRequiredData",
    "UserInputResponseRequest",
    "interaction_recorded_event",
]
