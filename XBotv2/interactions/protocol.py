"""Wire models owned by live client interactions."""

from typing import Any, Literal

from pydantic import Field, model_validator

from XBotv2.protocol import WireModel


class UserInputOption(WireModel):
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)


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


__all__ = [
    "InteractionRecordedData",
    "InteractionResponse",
    "UserInputOption",
    "UserInputRequiredData",
    "UserInputResponseRequest",
]
