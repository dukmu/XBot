"""Strict, versioned records stored by thread persistence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Annotated, TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from XBotv2.core.artifacts import ArtifactRef
from XBotv2.core.metadata import ThreadMetadata
from XBotv2.core.messages import ContentPart, ImageContent, Message

MESSAGE_SCHEMA_VERSION = 1
TRAJECTORY_SCHEMA_VERSION = 1
THREAD_LIFECYCLE_SCHEMA_VERSION = 1
INBOX_SCHEMA_VERSION = 1

if TYPE_CHECKING:
    from XBotv2.agentloop.inbox import InboxInput


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PersistenceRecord(BaseModel):
    """Strict field validation and JSON encoding for persisted records."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MessagePayloadRecord(PersistenceRecord):
    """The single persistence codec for a provider-neutral Message."""

    schema_version: Literal[1] = MESSAGE_SCHEMA_VERSION
    role: Literal["system", "user", "assistant", "tool"]
    status: str
    data: JsonValue
    parts: tuple[Annotated[ContentPart, Field(discriminator="type")], ...]
    tool_call_id: str
    input_id: str
    name: str
    additional_kwargs: dict[str, JsonValue]
    response_metadata: dict[str, JsonValue]
    usage_metadata: dict[str, JsonValue]
    artifact: tuple[ArtifactRef, ...]
    error: dict[str, JsonValue] | None

    @classmethod
    def from_message(cls, message: Message) -> "MessagePayloadRecord":
        return cls(
            role=message.role,
            status=message.status,
            data=message.data,
            parts=tuple(message.parts),
            tool_call_id=message.tool_call_id,
            input_id=message.input_id,
            name=message.name,
            additional_kwargs=message.additional_kwargs,
            response_metadata=message.response_metadata,
            usage_metadata=message.usage_metadata,
            artifact=tuple(message.artifact or ()),
            error=message.error,
        )

    def to_message(self) -> Message:
        return Message(
            role=self.role,
            parts=list(self.parts),
            status=self.status,
            data=self.data,
            tool_call_id=self.tool_call_id,
            input_id=self.input_id,
            name=self.name,
            additional_kwargs=self.additional_kwargs,
            response_metadata=self.response_metadata,
            usage_metadata=self.usage_metadata,
            artifact=list(self.artifact) or None,
            error=self.error,
        )


class MessageRecord(MessagePayloadRecord):
    """One append-origin message in the trajectory."""

    position: int = Field(ge=1)

    @classmethod
    def from_message(cls, message: Message, position: int) -> "MessageRecord":
        return cls(
            **MessagePayloadRecord.from_message(message).model_dump(),
            position=position,
        )


class SurfaceReplaceRecord(PersistenceRecord):
    """One deterministic replacement of current contiguous surface nodes."""

    schema_version: Literal[1] = TRAJECTORY_SCHEMA_VERSION
    position: int = Field(ge=1)
    record_type: Literal["surface_replace"] = "surface_replace"
    operation: str
    transcript: Literal["preserve", "replace"] = "replace"
    source_node_ids: tuple[str, ...]
    messages: tuple[MessagePayloadRecord, ...]

    @field_validator("operation", mode="before")
    @classmethod
    def _validate_operation(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Surface operation must be a non-empty string")
        return value

    @field_validator("source_node_ids", mode="before")
    @classmethod
    def _validate_source_nodes(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("Surface replacement requires source node ids")
        nodes = tuple(value)
        if any(not isinstance(node, str) or not node for node in nodes):
            raise ValueError("Surface source node ids must be non-empty strings")
        if len(set(nodes)) != len(nodes):
            raise ValueError("Surface source node ids must be unique")
        return nodes

class TrajectoryEventRecord(PersistenceRecord):
    """One plugin-owned, log-only event that never enters the surface."""

    schema_version: Literal[1] = TRAJECTORY_SCHEMA_VERSION
    position: int = Field(ge=1)
    record_type: Literal["event"] = "event"
    event: str
    data: dict[str, JsonValue]
    timestamp: str

    @field_validator("event", mode="before")
    @classmethod
    def _validate_event(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Trajectory event must be a non-empty string")
        return value

    @field_validator("timestamp", mode="before")
    @classmethod
    def _validate_timestamp(cls, value: object) -> str:
        return _timestamp(value, "timestamp")


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
    images: tuple[ImageContent, ...]
    artifacts: tuple[ArtifactRef, ...]
    metadata: dict[str, JsonValue]

    @classmethod
    def from_input(cls, item: "InboxInput") -> "InboxItemRecord":
        return cls(
            message_id=item.message_id,
            content=item.content,
            target=item.target.value,
            source=item.source,
            images=tuple(item.images),
            artifacts=tuple(item.artifacts),
            metadata=item.metadata,
        )

    def to_input(self) -> "InboxInput":
        from XBotv2.agentloop.inbox import InboxInput, InboxTarget

        return InboxInput(
            message_id=self.message_id,
            content=self.content,
            target=InboxTarget(self.target),
            source=self.source,
            images=list(self.images),
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

    def to_inputs(self) -> list["InboxInput"]:
        return [item.to_input() for item in self.items]


def _timestamp(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    timestamp = value
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return timestamp


__all__ = [
    "InboxItemRecord",
    "InboxSnapshot",
    "MessagePayloadRecord",
    "MessageRecord",
    "SurfaceReplaceRecord",
    "TrajectoryEventRecord",
    "ThreadLifecycleRecord",
    "ThreadMetadata",
]
