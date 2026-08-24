"""Provider-neutral message and model stream types."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from XBotv2.core.artifacts import ArtifactRef
from XBotv2.core.tools import JsonValue, ToolCall, ToolCallDelta


@dataclass(frozen=True, slots=True)
class ImageContent:
    """A session-relative image artifact attached to a message."""

    path: str
    media_type: str
    size: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ImageContent":
        return cls(
            path=str(value.get("path") or ""),
            media_type=str(value.get("media_type") or "application/octet-stream"),
            size=int(value.get("size") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "media_type": self.media_type,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class TextPart:
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


@dataclass(frozen=True, slots=True)
class ReasoningPart:
    text: str
    provider_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": "reasoning", "text": self.text}
        if self.provider_data:
            data["provider_data"] = self.provider_data
        return data


@dataclass(frozen=True, slots=True)
class ImagePart:
    image: ImageContent

    def to_dict(self) -> dict[str, Any]:
        return {"type": "image", **self.image.to_dict()}


@dataclass(frozen=True, slots=True)
class ToolCallPart:
    call: ToolCall

    def to_dict(self) -> dict[str, Any]:
        return self.call.to_dict()


ContentPart = TextPart | ReasoningPart | ImagePart | ToolCallPart


def merge_model_chunk(
    aggregate: "ModelResponse | None",
    chunk: "ModelChunk",
) -> "ModelResponse":
    """Merge one provider-neutral stream chunk into a response value."""
    if not isinstance(aggregate, ModelResponse):
        aggregate = ModelResponse()
    aggregate.reasoning += chunk.reasoning
    aggregate.content += chunk.content
    if chunk.tool_calls:
        aggregate.tool_calls = chunk.tool_calls
    if chunk.response_metadata:
        aggregate.response_metadata.update(chunk.response_metadata)
    if chunk.usage_metadata:
        aggregate.usage_metadata.update(chunk.usage_metadata)
    if chunk.additional_kwargs:
        aggregate.additional_kwargs.update(chunk.additional_kwargs)
    return aggregate


def part_from_dict(value: dict[str, Any]) -> ContentPart:
    part_type = value.get("type")
    if part_type == "text":
        return TextPart(str(value.get("text") or ""))
    if part_type == "reasoning":
        return ReasoningPart(
            str(value.get("text") or ""),
            dict(value.get("provider_data") or {}),
        )
    if part_type == "image":
        return ImagePart(ImageContent.from_dict(value))
    if part_type == "tool_call":
        return ToolCallPart(ToolCall.from_dict(value))
    raise ValueError(f"Unknown message content part: {part_type!r}")


def _content_parts(
    *,
    content: str = "",
    reasoning: str = "",
    images: list[ImageContent] | None = None,
    tool_calls: list[ToolCall] | None = None,
) -> list[ContentPart]:
    parts: list[ContentPart] = []
    if reasoning:
        parts.append(ReasoningPart(reasoning))
    if content:
        parts.append(TextPart(content))
    parts.extend(ImagePart(image) for image in images or [])
    parts.extend(ToolCallPart(call) for call in tool_calls or [])
    return parts


class _PartBacked:
    parts: list[ContentPart]

    @property
    def content(self) -> str:
        return "".join(
            part.text for part in self.parts if isinstance(part, TextPart)
        )

    @content.setter
    def content(self, value: str) -> None:
        index = next(
            (i for i, part in enumerate(self.parts) if isinstance(part, TextPart)),
            len(self.parts),
        )
        self.parts = [
            part for part in self.parts if not isinstance(part, TextPart)
        ]
        if value:
            self.parts.insert(min(index, len(self.parts)), TextPart(value))

    @property
    def reasoning(self) -> str:
        return "".join(
            part.text for part in self.parts if isinstance(part, ReasoningPart)
        )

    @reasoning.setter
    def reasoning(self, value: str) -> None:
        self.parts = [
            part for part in self.parts if not isinstance(part, ReasoningPart)
        ]
        if value:
            self.parts.insert(0, ReasoningPart(value))

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [
            part.call for part in self.parts if isinstance(part, ToolCallPart)
        ]

    @tool_calls.setter
    def tool_calls(self, value: list[ToolCall]) -> None:
        self.parts = [
            part for part in self.parts if not isinstance(part, ToolCallPart)
        ]
        self.parts.extend(ToolCallPart(call) for call in value)

    @property
    def images(self) -> list[ImageContent]:
        return [part.image for part in self.parts if isinstance(part, ImagePart)]


@dataclass(init=False)
class Message(_PartBacked):
    role: str
    parts: Sequence[ContentPart]
    tool_call_id: str
    input_id: str
    name: str
    status: str
    data: JsonValue
    additional_kwargs: dict[str, Any]
    response_metadata: dict[str, Any]
    usage_metadata: dict[str, Any]
    artifact: Any
    error: dict[str, Any] | None
    client_events: list[dict[str, Any]]
    turn_complete: bool
    _sealed: bool = field(default=False, init=False, repr=False, compare=False)

    def __init__(
        self,
        role: str = "",
        content: str = "",
        tool_calls: list[ToolCall] | None = None,
        tool_call_id: str = "",
        input_id: str = "",
        name: str = "",
        status: str = "",
        data: JsonValue = None,
        additional_kwargs: dict[str, Any] | None = None,
        response_metadata: dict[str, Any] | None = None,
        usage_metadata: dict[str, Any] | None = None,
        artifact: Any = None,
        images: list[ImageContent] | None = None,
        reasoning: str = "",
        parts: list[ContentPart] | None = None,
        error: dict[str, Any] | None = None,
        client_events: list[dict[str, Any]] | None = None,
        turn_complete: bool = False,
    ) -> None:
        self._sealed = False
        self.role = role
        self.tool_call_id = tool_call_id
        self.input_id = input_id
        self.name = name
        self.status = status
        self.data = data
        self.additional_kwargs = dict(additional_kwargs or {})
        self.response_metadata = dict(response_metadata or {})
        self.usage_metadata = dict(usage_metadata or {})
        self.artifact = artifact
        self.error = dict(error) if error is not None else None
        self.client_events = list(client_events or [])
        self.turn_complete = turn_complete
        if parts is not None:
            self.parts = list(parts)
            return
        self.parts = _content_parts(
            content=content,
            reasoning=reasoning,
            images=images,
            tool_calls=tool_calls,
        )

    def __setattr__(self, name: str, value: object) -> None:
        if name != "_sealed" and self._sealed:
            raise RuntimeError("A message in ConversationHistory is immutable")
        object.__setattr__(self, name, value)

    def seal(self) -> None:
        if self._sealed:
            return
        self.parts = _FrozenList(_freeze_part(part) for part in self.parts)
        self.additional_kwargs = _freeze_object(self.additional_kwargs)
        self.response_metadata = _freeze_object(self.response_metadata)
        self.usage_metadata = _freeze_object(self.usage_metadata)
        self.data = _freeze_json(self.data)
        self.artifact = _freeze_artifact(self.artifact)
        self.error = _freeze_object(self.error) if self.error is not None else None
        self.client_events = _FrozenList(
            _freeze_object(event) for event in self.client_events
        )
        self._sealed = True


class _FrozenDict(dict[str, Any]):
    def _immutable(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("A message in ConversationHistory is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __deepcopy__(self, _memo: dict[int, object]) -> "_FrozenDict":
        return self


class _FrozenList(list[Any]):
    def _immutable(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("A message in ConversationHistory is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable

    def __deepcopy__(self, _memo: dict[int, object]) -> "_FrozenList":
        return self


def _freeze_part(part: ContentPart) -> ContentPart:
    if isinstance(part, ReasoningPart):
        return ReasoningPart(part.text, _freeze_object(part.provider_data))
    if isinstance(part, ToolCallPart):
        return ToolCallPart(ToolCall(
            part.call.id,
            part.call.name,
            _freeze_object(part.call.args),
        ))
    return part


def _freeze_object(value: Mapping[str, object]) -> _FrozenDict:
    return _FrozenDict({key: _freeze_json(item) for key, item in value.items()})


def _freeze_json(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return _FrozenList(_freeze_json(item) for item in value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Message JSON object keys must be strings")
        return _freeze_object(value)
    raise TypeError(
        f"Message persisted fields must be JSON-compatible, got {type(value).__name__}"
    )


def _freeze_artifact(value: object) -> object:
    if isinstance(value, ArtifactRef):
        return value
    if isinstance(value, (list, tuple)):
        return _FrozenList(_freeze_artifact(item) for item in value)
    if isinstance(value, Mapping):
        return _freeze_object(value)
    if value is None:
        return None
    raise TypeError(f"Unsupported message artifact value: {type(value).__name__}")

@dataclass(init=False)
class ModelResponse(_PartBacked):
    parts: list[ContentPart]
    response_metadata: dict[str, Any]
    usage_metadata: dict[str, Any]
    additional_kwargs: dict[str, Any]

    def __init__(
        self,
        content: str = "",
        tool_calls: list[ToolCall] | None = None,
        response_metadata: dict[str, Any] | None = None,
        usage_metadata: dict[str, Any] | None = None,
        additional_kwargs: dict[str, Any] | None = None,
        reasoning: str = "",
        parts: list[ContentPart] | None = None,
    ) -> None:
        self.response_metadata = dict(response_metadata or {})
        self.usage_metadata = dict(usage_metadata or {})
        self.additional_kwargs = dict(additional_kwargs or {})
        self.parts = (
            list(parts)
            if parts is not None
            else _content_parts(
                content=content,
                reasoning=reasoning,
                tool_calls=tool_calls,
            )
        )


@dataclass
class ModelChunk:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_chunks: list[ToolCallDelta] = field(default_factory=list)
    response_metadata: dict[str, Any] = field(default_factory=dict)
    usage_metadata: dict[str, Any] = field(default_factory=dict)
    additional_kwargs: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "ContentPart",
    "ImageContent",
    "ImagePart",
    "Message",
    "ModelChunk",
    "ModelResponse",
    "ReasoningPart",
    "TextPart",
    "ToolCallPart",
    "part_from_dict",
]
