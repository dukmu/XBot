"""Strict, versioned records stored by thread persistence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, cast

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


@dataclass(frozen=True, slots=True)
class MessageRecord:
    schema_version: int
    position: int
    role: str
    status: str
    data: JsonValue
    parts: tuple[JsonObject, ...]
    tool_call_id: str
    input_id: str
    name: str
    additional_kwargs: JsonObject
    response_metadata: JsonObject
    usage_metadata: JsonObject
    artifact: JsonValue
    error: JsonObject | None

    @classmethod
    def from_message(cls, message: Message, position: int) -> "MessageRecord":
        if message.role not in {"system", "user", "assistant", "tool"}:
            raise ValueError(f"Unsupported persisted message role: {message.role!r}")
        if position < 1:
            raise ValueError("position must be positive")
        return cls(
            schema_version=MESSAGE_SCHEMA_VERSION,
            position=position,
            role=message.role,
            status=_string(message.status, "status"),
            data=json_value(message.data),
            parts=tuple(
                _message_part(part.to_dict()) for part in message.parts
            ),
            tool_call_id=_string(message.tool_call_id, "tool_call_id"),
            input_id=_string(message.input_id, "input_id"),
            name=_string(message.name, "name"),
            additional_kwargs=json_object(message.additional_kwargs),
            response_metadata=json_object(message.response_metadata),
            usage_metadata=json_object(message.usage_metadata),
            artifact=_artifact_value(message.artifact),
            error=(
                json_object(message.error)
                if message.error is not None
                else None
            ),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "MessageRecord":
        expected = {
            "schema_version", "position", "role", "status", "data",
            "parts", "tool_call_id", "input_id", "name", "additional_kwargs",
            "response_metadata", "usage_metadata", "artifact", "error",
        }
        _exact_fields(value, expected, "MessageRecord")
        version = _integer(value["schema_version"], "schema_version", minimum=1)
        if version != MESSAGE_SCHEMA_VERSION:
            raise ValueError(f"Unsupported MessageRecord schema version: {version}")
        raw_parts = value["parts"]
        if not isinstance(raw_parts, list):
            raise TypeError("MessageRecord.parts must be a list")
        error = value["error"]
        if error is not None and not isinstance(error, Mapping):
            raise TypeError("MessageRecord.error must be an object or null")
        artifact = json_value(value["artifact"])
        return cls(
            schema_version=version,
            position=_integer(value["position"], "position", minimum=1),
            role=_role(value["role"]),
            status=_string(value["status"], "status"),
            data=json_value(value["data"]),
            parts=tuple(_message_part(part) for part in raw_parts),
            tool_call_id=_string(value["tool_call_id"], "tool_call_id"),
            input_id=_string(value["input_id"], "input_id"),
            name=_string(value["name"], "name"),
            additional_kwargs=_object(value["additional_kwargs"], "additional_kwargs"),
            response_metadata=_object(value["response_metadata"], "response_metadata"),
            usage_metadata=_object(value["usage_metadata"], "usage_metadata"),
            artifact=artifact,
            error=json_object(error) if isinstance(error, Mapping) else None,
        )

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

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "position": self.position,
            "role": self.role,
            "status": self.status,
            "data": self.data,
            "parts": [dict(part) for part in self.parts],
            "tool_call_id": self.tool_call_id,
            "input_id": self.input_id,
            "name": self.name,
            "additional_kwargs": self.additional_kwargs,
            "response_metadata": self.response_metadata,
            "usage_metadata": self.usage_metadata,
            "artifact": self.artifact,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class ThreadLifecycleRecord:
    schema_version: int
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
            schema_version=THREAD_LIFECYCLE_SCHEMA_VERSION,
            event=event,
            thread_id=_string(thread_id, "thread_id"),
            parent_thread_id=_string(parent_thread_id, "parent_thread_id"),
            agent=_string(agent, "agent"),
            timestamp=utc_now(),
            error=_string(error, "error"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ThreadLifecycleRecord":
        expected = {
            "schema_version", "event", "thread_id", "parent_thread_id",
            "agent", "timestamp", "error",
        }
        _exact_fields(value, expected, "ThreadLifecycleRecord")
        version = _integer(value["schema_version"], "schema_version", minimum=1)
        if version != THREAD_LIFECYCLE_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported ThreadLifecycleRecord schema version: {version}"
            )
        event = _string(value["event"], "event")
        if event not in {"started", "completed", "failed", "cancelled"}:
            raise ValueError(f"Unsupported thread lifecycle event: {event!r}")
        return cls(
            schema_version=version,
            event=cast(
                Literal["started", "completed", "failed", "cancelled"], event
            ),
            thread_id=_string(value["thread_id"], "thread_id"),
            parent_thread_id=_string(value["parent_thread_id"], "parent_thread_id"),
            agent=_string(value["agent"], "agent"),
            timestamp=_timestamp(value["timestamp"], "timestamp"),
            error=_string(value["error"], "error"),
        )

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "event": self.event,
            "thread_id": self.thread_id,
            "parent_thread_id": self.parent_thread_id,
            "agent": self.agent,
            "timestamp": self.timestamp,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class InboxItemRecord:
    message_id: str
    content: str
    target: Literal["next-turn", "next-step"]
    source: str
    images: tuple[JsonObject, ...]
    artifacts: tuple[ArtifactRef, ...]
    metadata: JsonObject

    @classmethod
    def from_input(cls, item: "InboxInput") -> "InboxItemRecord":
        return cls(
            message_id=_string(item.message_id, "message_id"),
            content=_string(item.content, "content"),
            target=item.target.value,
            source=_string(item.source, "source"),
            images=tuple(_image(image.to_dict()) for image in item.images),
            artifacts=tuple(item.artifacts),
            metadata=json_object(item.metadata),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "InboxItemRecord":
        expected = {
            "message_id", "content", "target", "source", "images",
            "artifacts", "metadata",
        }
        _exact_fields(value, expected, "InboxItemRecord")
        target = _string(value["target"], "target")
        if target not in {"next-turn", "next-step"}:
            raise ValueError(f"Unsupported inbox target: {target!r}")
        images = value["images"]
        artifacts = value["artifacts"]
        if not isinstance(images, list):
            raise TypeError("InboxItemRecord.images must be a list")
        if not isinstance(artifacts, list):
            raise TypeError("InboxItemRecord.artifacts must be a list")
        return cls(
            message_id=_string(value["message_id"], "message_id"),
            content=_string(value["content"], "content"),
            target=target,
            source=_string(value["source"], "source"),
            images=tuple(_image(image) for image in images),
            artifacts=tuple(
                ArtifactRef.from_dict(_mapping(artifact, "artifact"))
                for artifact in artifacts
            ),
            metadata=_object(value["metadata"], "metadata"),
        )

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

    def to_dict(self) -> JsonObject:
        return {
            "message_id": self.message_id,
            "content": self.content,
            "target": self.target,
            "source": self.source,
            "images": [dict(image) for image in self.images],
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class InboxSnapshot:
    schema_version: int
    items: tuple[InboxItemRecord, ...]

    @classmethod
    def from_inputs(cls, items: "Sequence[InboxInput]") -> "InboxSnapshot":
        return cls(
            schema_version=INBOX_SCHEMA_VERSION,
            items=tuple(InboxItemRecord.from_input(item) for item in items),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "InboxSnapshot":
        _exact_fields(value, {"schema_version", "items"}, "InboxSnapshot")
        version = _integer(value["schema_version"], "schema_version", minimum=1)
        if version != INBOX_SCHEMA_VERSION:
            raise ValueError(f"Unsupported InboxSnapshot schema version: {version}")
        items = value["items"]
        if not isinstance(items, list):
            raise TypeError("InboxSnapshot.items must be a list")
        return cls(
            schema_version=version,
            items=tuple(
                InboxItemRecord.from_dict(_mapping(item, "inbox item"))
                for item in items
            ),
        )

    def to_inputs(self) -> list["InboxInput"]:
        return [item.to_input() for item in self.items]

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "items": [item.to_dict() for item in self.items],
        }


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


def _role(value: object) -> str:
    role = _string(value, "role")
    if role not in {"system", "user", "assistant", "tool"}:
        raise ValueError(f"Unsupported persisted message role: {role!r}")
    return role


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
