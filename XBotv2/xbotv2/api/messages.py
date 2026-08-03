"""Provider-neutral message and model stream types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from xbotv2.api.tools import ToolCall, ToolCallDelta


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
    parts: list[ContentPart]
    tool_call_id: str
    name: str
    status: str
    additional_kwargs: dict[str, Any]
    response_metadata: dict[str, Any]
    usage_metadata: dict[str, Any]
    artifact: Any

    def __init__(
        self,
        role: str = "",
        content: str = "",
        tool_calls: list[ToolCall] | None = None,
        tool_call_id: str = "",
        name: str = "",
        status: str = "",
        additional_kwargs: dict[str, Any] | None = None,
        response_metadata: dict[str, Any] | None = None,
        usage_metadata: dict[str, Any] | None = None,
        artifact: Any = None,
        images: list[ImageContent] | None = None,
        reasoning: str = "",
        parts: list[ContentPart] | None = None,
    ) -> None:
        self.role = role
        self.tool_call_id = tool_call_id
        self.name = name
        self.status = status
        self.additional_kwargs = dict(additional_kwargs or {})
        self.response_metadata = dict(response_metadata or {})
        self.usage_metadata = dict(usage_metadata or {})
        self.artifact = artifact
        if parts is not None:
            self.parts = list(parts)
            return
        self.parts = _content_parts(
            content=content,
            reasoning=reasoning,
            images=images,
            tool_calls=tool_calls,
        )

    def fingerprint(self) -> int:
        """Cheap stable fingerprint for persisted-message change detection.

        ``str`` hashes are cached, so fingerprinting large message content is
        much cheaper than serializing it while still catching in-place edits.
        """
        return hash((
            self.role,
            str(self.content or ""),
            self.tool_call_id,
            self.status,
            self.name,
            len(self.parts),
            len(self.additional_kwargs or {}),
            len(self.usage_metadata or {}),
            len(self.response_metadata or {}),
        ))


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
