"""Wire models owned by live client interactions."""

from typing import Any, Literal

from pydantic import Field, model_validator

from XBotv2.protocol import WireModel
from XBotv2.core import ClientEvent
from XBotv2.core.tools import _validated_client_event


class UserInputOption(WireModel):
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)


class ClientMessageData(WireModel):
    message: str = Field(min_length=1)
    level: Literal["info", "warning", "error"] = "info"
    source: str = Field(min_length=1)
    tool_call_id: str = ""


class UserInputRequiredData(WireModel):
    request_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    options: list[UserInputOption] = Field(default_factory=list)
    timeout_seconds: float | None = Field(default=None, gt=0)
    resume_supported: bool = False

    @model_validator(mode="after")
    def _validate_ask_user_options(self) -> "UserInputRequiredData":
        if self.source == "ask_user" and len(self.options) < 2:
            raise ValueError("ask_user requires at least two options")
        return self


class UserInputResponseRequest(WireModel):
    request_id: str = Field(min_length=1)
    answer: Any = None


class InteractionRecordedData(WireModel):
    request_id: str = Field(min_length=1)
    status: Literal["answered", "timeout", "cancelled"]
    decision: Literal["allow", "deny", ""] = ""
    scope: Literal["once", "session", ""] = ""
    answer: Any = None
    pending_interactions: list[str] = Field(default_factory=list)


class InteractionResponse(WireModel):
    request_id: str = Field(min_length=1)
    recorded: Literal[True] = True
    pending_interactions: list[str] = Field(default_factory=list)


InteractionEventType = Literal[
    "permission_response_recorded",
    "user_input_recorded",
]


def interaction_recorded_event(
    type: InteractionEventType,
    data: dict[str, Any],
) -> ClientEvent:
    """Validate a recorded interaction before publishing it."""
    return _validated_client_event(type, data, InteractionRecordedData)


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
