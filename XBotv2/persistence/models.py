"""Strict, versioned records stored by thread persistence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from XBotv2.core.artifacts import ArtifactRef
from XBotv2.core.metadata import ThreadMetadata
from XBotv2.core.messages import ImageContent, Message, part_from_dict
from XBotv2.core.tools import JsonObject, JsonValue, json_object, json_value

MESSAGE_SCHEMA_VERSION = 1
THREAD_LIFECYCLE_SCHEMA_VERSION = 1
INBOX_SCHEMA_VERSION = 1

if TYPE_CHECKING:
    from XBotv2.agentloop.inbox import InboxInput


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PersistenceRecord(BaseModel):
    """Strict field validation and JSON encoding for persisted records."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, arbitrary_types_allowed=True
    )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        _exact_fields(value, set(cls.model_fields), cls.__name__)
        return cls.model_validate(value)

    def to_dict(self) -> JsonObject:
        return {
            name: _persisted_value(getattr(self, name))
            for name in type(self).model_fields
        }


class MessageRecord(PersistenceRecord):
    schema_version: Literal[1] = MESSAGE_SCHEMA_VERSION
    position: int = Field(ge=1)
    role: Literal["system", "user", "assistant", "tool"]
    status: str
    data: Any
    parts: tuple[dict[str, Any], ...]
    tool_call_id: str
    input_id: str
    name: str
    additional_kwargs: dict[str, Any]
    response_metadata: dict[str, Any]
    usage_metadata: dict[str, Any]
    artifact: Any
    error: dict[str, Any] | None

    @classmethod
    def from_message(cls, message: Message, position: int) -> "MessageRecord":
        return cls(
            position=position,
            role=message.role,
            status=message.status,
            data=message.data,
            parts=tuple(part.to_dict() for part in message.parts),
            tool_call_id=message.tool_call_id,
            input_id=message.input_id,
            name=message.name,
            additional_kwargs=message.additional_kwargs,
            response_metadata=message.response_metadata,
            usage_metadata=message.usage_metadata,
            artifact=_artifact_value(message.artifact),
            error=message.error,
        )

    @field_validator("parts", mode="before")
    @classmethod
    def _validate_parts(cls, value: object) -> tuple[JsonObject, ...]:
        if not isinstance(value, (list, tuple)):
            raise TypeError("MessageRecord.parts must be a list")
        return tuple(_message_part(part) for part in value)

    @field_validator("data", "artifact", mode="before")
    @classmethod
    def _validate_value(cls, value: object) -> JsonValue:
        return json_value(value)

    @field_validator(
        "additional_kwargs", "response_metadata", "usage_metadata", mode="before"
    )
    @classmethod
    def _validate_object(cls, value: object) -> JsonObject:
        return _object(value, "message metadata")

    @field_validator("error", mode="before")
    @classmethod
    def _validate_error(cls, value: object) -> JsonObject | None:
        return None if value is None else _object(value, "error")

    def to_message(self) -> Message:
        return Message(
            role=self.role,
            parts=[part_from_dict(dict(part)) for part in self.parts],
            status=self.status,
            data=self.data,
            tool_call_id=self.tool_call_id,
            input_id=self.input_id,
            name=self.name,
            additional_kwargs=self.additional_kwargs,
            response_metadata=self.response_metadata,
            usage_metadata=self.usage_metadata,
            artifact=_restore_artifacts(self.artifact),
            error=self.error,
        )


class ThreadLifecycleRecord(PersistenceRecord):
    schema_version: Literal[1] = THREAD_LIFECYCLE_SCHEMA_VERSION
    event: Literal["started", "completed", "failed", "cancelled"]
    thread_id: str
    parent_thread_id: str
    agent: str
    timestamp: str
    error: str = ""

    @classmethod
    def create(
        cls,
        event: Literal["started", "completed", "failed", "cancelled"],
        *,
        thread_id: str,
        parent_thread_id: str,
        agent: str,
        error: str = "",
    ) -> "ThreadLifecycleRecord":
        if event not in {"started", "completed", "failed", "cancelled"}:
            raise ValueError(f"Unsupported thread lifecycle event: {event!r}")
        return cls(
            event=event,
            thread_id=thread_id,
            parent_thread_id=parent_thread_id,
            agent=agent,
            timestamp=utc_now(),
            error=error,
        )

    @field_validator("timestamp", mode="before")
    @classmethod
    def _validate_timestamp(cls, value: object) -> str:
        return _timestamp(value, "timestamp")


class InboxItemRecord(PersistenceRecord):
    message_id: str
    content: str
    target: Literal["next-turn", "next-step"]
    source: str
    images: tuple[dict[str, Any], ...]
    artifacts: tuple[ArtifactRef, ...]
    metadata: dict[str, Any]

    @classmethod
    def from_input(cls, item: "InboxInput") -> "InboxItemRecord":
        return cls(
            message_id=item.message_id,
            content=item.content,
            target=item.target.value,
            source=item.source,
            images=tuple(image.to_dict() for image in item.images),
            artifacts=tuple(item.artifacts),
            metadata=item.metadata,
        )

    @field_validator("images", mode="before")
    @classmethod
    def _validate_images(cls, value: object) -> tuple[JsonObject, ...]:
        if not isinstance(value, (list, tuple)):
            raise TypeError("InboxItemRecord.images must be a list")
        return tuple(_image(image) for image in value)

    @field_validator("artifacts", mode="before")
    @classmethod
    def _validate_artifacts(cls, value: object) -> tuple[ArtifactRef, ...]:
        if not isinstance(value, (list, tuple)):
            raise TypeError("InboxItemRecord.artifacts must be a list")
        return tuple(
            artifact
            if isinstance(artifact, ArtifactRef)
            else ArtifactRef.from_dict(_mapping(artifact, "artifact"))
            for artifact in value
        )

    @field_validator("metadata", mode="before")
    @classmethod
    def _validate_metadata(cls, value: object) -> JsonObject:
        return _object(value, "metadata")

    def to_input(self) -> "InboxInput":
        from XBotv2.agentloop.inbox import InboxInput, InboxTarget

        return InboxInput(
            message_id=self.message_id,
            content=self.content,
            target=InboxTarget(self.target),
            source=self.source,
            images=[ImageContent.from_dict(image) for image in self.images],
            artifacts=list(self.artifacts),
            metadata=self.metadata,
        )


class InboxSnapshot(PersistenceRecord):
    schema_version: Literal[1] = INBOX_SCHEMA_VERSION
    items: tuple[InboxItemRecord, ...]

    @classmethod
    def from_inputs(cls, items: "Sequence[InboxInput]") -> "InboxSnapshot":
        return cls(
            schema_version=INBOX_SCHEMA_VERSION,
            items=tuple(InboxItemRecord.from_input(item) for item in items),
        )

    @field_validator("items", mode="before")
    @classmethod
    def _validate_items(cls, value: object) -> tuple[InboxItemRecord, ...]:
        if not isinstance(value, (list, tuple)):
            raise TypeError("InboxSnapshot.items must be a list")
        return tuple(
            item
            if isinstance(item, InboxItemRecord)
            else InboxItemRecord.from_dict(_mapping(item, "inbox item"))
            for item in value
        )

    def to_inputs(self) -> list["InboxInput"]:
        return [item.to_input() for item in self.items]


def _message_part(value: object) -> JsonObject:
    if not isinstance(value, Mapping):
        raise TypeError("Message part must be an object")
    part = json_object(value)
    part_type = part.get("type")
    allowed_fields = {
        "text": ({"type", "text"},),
        "reasoning": (
            {"type", "text"},
            {"type", "text", "provider_data"},
        ),
        "image": ({"type", "path", "media_type", "size"},),
        "tool_call": ({"type", "id", "name", "args"},),
    }.get(part_type)
    if allowed_fields is None:
        raise ValueError(f"Unknown message part type: {part_type!r}")
    if set(part) not in allowed_fields:
        raise ValueError(f"Invalid fields for {part_type} message part")
    if part_type in {"text", "reasoning"}:
        _string(part["text"], f"{part_type}.text")
    if part_type == "reasoning" and "provider_data" in part:
        _object(part["provider_data"], "reasoning.provider_data")
    if part_type == "image":
        _string(part["path"], "image.path")
        _string(part["media_type"], "image.media_type")
        _integer(part["size"], "image.size")
    if part_type == "tool_call":
        _string(part["id"], "tool_call.id")
        _string(part["name"], "tool_call.name")
        _object(part["args"], "tool_call.args")
    part_from_dict(part)
    return part


def _image(value: object) -> JsonObject:
    image = _mapping(value, "image")
    _exact_fields(image, {"path", "media_type", "size"}, "ImageContent")
    _string(image["path"], "image.path")
    _string(image["media_type"], "image.media_type")
    _integer(image["size"], "image.size")
    return json_object(image)


def _artifact_value(value: object) -> JsonValue:
    if isinstance(value, ArtifactRef):
        return value.to_dict()
    if isinstance(value, (list, tuple)):
        return [_artifact_value(item) for item in value]
    return json_value(value)


def _restore_artifacts(value: JsonValue) -> object:
    if isinstance(value, list):
        return [_restore_artifacts(item) for item in value]
    if isinstance(value, dict) and set(value) == {
        "id", "kind", "media_type", "name", "size", "sha256",
    }:
        return ArtifactRef.from_dict(value)
    return value


def _persisted_value(value: object) -> JsonValue:
    if isinstance(value, PersistenceRecord):
        return value.to_dict()
    if isinstance(value, ArtifactRef):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_persisted_value(item) for item in value]
    return json_value(value)


def _exact_fields(
    value: Mapping[str, object],
    expected: set[str],
    name: str,
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ValueError(f"{name} fields mismatch; missing={missing}, unknown={unknown}")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise TypeError(f"{name} must be an integer >= {minimum}")
    return value


def _timestamp(value: object, name: str) -> str:
    timestamp = _string(value, name)
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return timestamp


def _object(value: object, name: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return json_object(value)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"{name} keys must be strings")
    return value


__all__ = [
    "InboxItemRecord",
    "InboxSnapshot",
    "MessageRecord",
    "ThreadLifecycleRecord",
    "ThreadMetadata",
]
