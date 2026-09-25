"""Wire models owned by live client interactions."""

from typing import Literal, TypeAlias

from pydantic import Field, JsonValue, model_validator

from XBotv2.protocol import WireModel


class UserInputOption(WireModel):
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)


class UserInputRequest(WireModel):
    kind: Literal["user_input_required"] = "user_input_required"
    interaction_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    tool_call_id: str = ""
    question: str = Field(min_length=1)
    options: tuple[UserInputOption, ...] = ()
    timeout_seconds: float | None = Field(default=None, gt=0)
    resume_supported: bool = False

    @model_validator(mode="after")
    def _validate_ask_user_options(self) -> "UserInputRequest":
        if self.source == "ask_user" and len(self.options) < 2:
            raise ValueError("ask_user requires at least two options")
        return self


class Answered(WireModel):
    kind: Literal["answered"] = "answered"
    answer: JsonValue


class InputTimedOut(WireModel):
    kind: Literal["timeout"] = "timeout"
    reason: str = Field(min_length=1)


class InputCancelled(WireModel):
    kind: Literal["cancelled"] = "cancelled"
    reason: str = Field(min_length=1)


UserInputResolution: TypeAlias = Answered | InputTimedOut | InputCancelled


class ClientNotice(WireModel):
    kind: Literal["client_message"] = "client_message"
    message: str = Field(min_length=1)
    level: Literal["info", "warning", "error"] = "info"
    source: str = Field(min_length=1)
    tool_call_id: str = ""


class UserInputResponseRequest(WireModel):
    request_id: str = Field(min_length=1)
    answer: JsonValue = None


class UserInputRecorded(WireModel):
    kind: Literal["user_input_recorded"] = "user_input_recorded"
    interaction_id: str = Field(min_length=1)
    resolution: UserInputResolution
    pending_ids: tuple[str, ...] = ()


class InteractionResponse(WireModel):
    request_id: str = Field(min_length=1)
    recorded: Literal[True] = True
    pending_interactions: list[str] = Field(default_factory=list)


__all__ = [
    "ClientNotice",
    "UserInputRequest",
    "Answered",
    "InputTimedOut",
    "InputCancelled",
    "InteractionResponse",
    "UserInputOption",
    "UserInputResponseRequest",
    "UserInputRecorded",
    "UserInputResolution",
]
