"""Typed requests, resolutions, and events owned by interactions."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class _InteractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UserInputOption(_InteractionModel):
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)


class UserInputRequest(_InteractionModel):
    kind: Literal["user_input_required"] = "user_input_required"
    interaction_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    tool_call_id: str = ""
    question: str = Field(min_length=1)
    options: tuple[UserInputOption, ...] = ()
    timeout_seconds: float | None = Field(default=None, gt=0)
    resume_supported: bool = False


class Answered(_InteractionModel):
    kind: Literal["answered"] = "answered"
    answer: JsonValue


class InputTimedOut(_InteractionModel):
    kind: Literal["timeout"] = "timeout"
    reason: str = Field(min_length=1)


class InputCancelled(_InteractionModel):
    kind: Literal["cancelled"] = "cancelled"
    reason: str = Field(min_length=1)


UserInputResolution: TypeAlias = Answered | InputTimedOut | InputCancelled


class ClientNotice(_InteractionModel):
    kind: Literal["client_message"] = "client_message"
    message: str = Field(min_length=1)
    level: Literal["info", "warning", "error"] = "info"
    source: str = Field(min_length=1)
    tool_call_id: str = ""


class UserInputRecorded(_InteractionModel):
    kind: Literal["user_input_recorded"] = "user_input_recorded"
    interaction_id: str = Field(min_length=1)
    resolution: UserInputResolution
    pending_ids: tuple[str, ...] = ()


__all__ = [
    "Answered",
    "ClientNotice",
    "InputCancelled",
    "InputTimedOut",
    "UserInputOption",
    "UserInputRecorded",
    "UserInputRequest",
    "UserInputResolution",
]
