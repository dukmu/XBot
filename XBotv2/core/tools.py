"""Tool definition, invocation, and result API."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal, get_args, get_origin, get_type_hints

if TYPE_CHECKING:
    from XBotv2.core.messages import ImageContent

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject = dict[str, JsonValue]


@dataclass(frozen=True)
class GuardDecision:
    """Monotonic denial returned by a tool-execution guard plugin.

    Guards are registered on ``ctx.tools`` (``ToolsService.guard``) and
    evaluated in registration order before dispatch. ``None`` means the guard
    does not gate this call. A denial cannot be reversed by another guard.
    """

    action: Literal["deny"] = "deny"
    reason: str = ""
    source: str = "guard"
    client_events: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, default_id: str = "") -> "ToolCall":
        return cls(
            id=str(value.get("id") or default_id),
            name=str(value.get("name") or ""),
            args=dict(value.get("args") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "args": self.args, "type": "tool_call"}


@dataclass(frozen=True)
class ToolCallDelta:
    index: int
    id: str = ""
    name: str = ""
    args: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "id": self.id,
            "name": self.name,
            "args": self.args,
            "type": "tool_call_chunk",
        }


@dataclass(frozen=True)
class ToolError:
    code: str
    message: str
    retryable: bool = False
    details: dict[str, JsonValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


@dataclass(frozen=True)
class ClientEvent:
    type: str
    data: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.type:
            raise ValueError("client event type must not be empty")
        object.__setattr__(self, "data", json_object(self.data))

    @classmethod
    def from_mapping(cls, event: Mapping[str, object]) -> "ClientEvent":
        event_type = event.get("type")
        data = event.get("data")
        if not isinstance(event_type, str) or not event_type:
            raise TypeError("client event requires a non-empty string type")
        if not isinstance(data, Mapping):
            raise TypeError("client event data must be an object")
        return cls(type=event_type, data=json_object(data))

    def to_dict(self) -> JsonObject:
        return {"type": self.type, "data": json_object(self.data)}


def json_object(value: Mapping[object, object]) -> JsonObject:
    """Copy one mapping while enforcing the core JSON value contract."""
    result: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("JSON object keys must be strings")
        result[key] = _json_value(item)
    return result


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return json_object(value)
    raise TypeError(f"value must be JSON-compatible, got {type(value).__name__}")


@dataclass(frozen=True)
class ArtifactRef:
    id: str
    media_type: str = "application/octet-stream"
    name: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "media_type": self.media_type,
            "name": self.name,
        }


@dataclass(frozen=True)
class ToolResult:
    status: Literal["success", "error", "denied", "cancelled"] = "success"
    content: str = ""
    error: ToolError | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    images: tuple["ImageContent", ...] = ()
    client_events: tuple[ClientEvent, ...] = ()
    turn_complete: bool = False

    @classmethod
    def success(
        cls,
        content: str = "",
        *,
        images: tuple["ImageContent", ...] = (),
    ) -> "ToolResult":
        return cls(content=content, images=images)

    @classmethod
    def failure(
        cls, code: str, message: str, *, retryable: bool = False
    ) -> "ToolResult":
        return cls(
            status="error",
            content=message,
            error=ToolError(code, message, retryable),
        )


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    function: Callable[..., Any]
    parameters: dict[str, Any]
    injected_parameters: tuple[str, ...] = ()

    @classmethod
    def from_function(cls, function: Callable[..., Any], *, name: str | None = None) -> "Tool":
        signature = inspect.signature(function)
        description = (inspect.getdoc(function) or "").strip()
        try:
            type_hints = get_type_hints(function)
        except (NameError, TypeError):
            type_hints = {}
        injected = tuple(
            parameter_name
            for parameter_name, parameter in signature.parameters.items()
            if parameter.kind == parameter.KEYWORD_ONLY
            and parameter.default is not inspect.Parameter.empty
        )
        return cls(
            name=name or function.__name__,
            description=description,
            function=function,
            parameters=_parameters_schema(signature, type_hints),
            injected_parameters=injected,
        )

    def invoke(self, args: dict[str, Any], **injected: Any) -> Any:
        result = self.function(**args, **self._injected(injected))
        if inspect.isawaitable(result):
            import asyncio

            return asyncio.run(result)
        return result

    async def ainvoke(self, args: dict[str, Any], **injected: Any) -> Any:
        kwargs = {**args, **self._injected(injected)}
        if inspect.iscoroutinefunction(self.function):
            return await self.function(**kwargs)

        import asyncio

        result = await asyncio.to_thread(self.function, **kwargs)
        return await result if inspect.isawaitable(result) else result

    def provider_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def _injected(self, values: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in values.items() if key in self.injected_parameters}


def provider_tool_schema(tool: Any) -> Any:
    if isinstance(tool, Tool):
        return tool.provider_schema()
    if hasattr(tool, "provider_schema"):
        return tool.provider_schema()
    return tool


def tool_parameters_schema(tool: Any) -> dict[str, Any]:
    """Return one JSON Schema for XBot and compatible external tools."""
    if isinstance(tool, Tool):
        return tool.parameters
    args_schema = getattr(tool, "args_schema", None)
    if hasattr(args_schema, "model_json_schema"):
        return args_schema.model_json_schema()
    if isinstance(args_schema, dict):
        return args_schema
    properties = getattr(tool, "args", None)
    if isinstance(properties, dict):
        return {"type": "object", "properties": properties}
    return {"type": "object", "properties": {}}


def _parameters_schema(
    signature: inspect.Signature,
    type_hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    accepts_extra = False
    for name, parameter in signature.parameters.items():
        if parameter.kind == parameter.VAR_KEYWORD:
            accepts_extra = True
            continue
        if parameter.kind == parameter.VAR_POSITIONAL:
            continue
        if parameter.kind == parameter.KEYWORD_ONLY and parameter.default is not inspect.Parameter.empty:
            continue
        annotation = (type_hints or {}).get(name, parameter.annotation)
        properties[name] = _annotation_schema(annotation)
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": accepts_extra,
    }
    if required:
        schema["required"] = required
    return schema


def _annotation_schema(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Signature.empty:
        return {"type": "string"}
    if annotation is Any:
        return {}
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Literal:
        values = list(args)
        value_type = type(values[0]) if values else str
        schema = _annotation_schema(value_type)
        schema["enum"] = values
        return schema
    if origin is list:
        return {"type": "array", "items": _annotation_schema(args[0] if args else str)}
    if origin is dict:
        value_type = args[1] if len(args) > 1 else Any
        return {
            "type": "object",
            "additionalProperties": _annotation_schema(value_type),
        }
    if origin in {tuple, set}:
        return {"type": "object"}
    if origin is not None and type(None) in args:
        non_null = [
            _annotation_schema(arg)
            for arg in args
            if arg is not type(None)
        ]
        return {"anyOf": [*non_null, {"type": "null"}]}
    return {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
    }.get(annotation, {"type": "string"})


__all__ = [
    "ArtifactRef",
    "ClientEvent",
    "JsonObject",
    "JsonValue",
    "Tool",
    "ToolCall",
    "ToolCallDelta",
    "ToolError",
    "ToolResult",
    "json_object",
    "tool_parameters_schema",
    "provider_tool_schema",
]
