"""Tool definition, invocation, and result API."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, get_args, get_origin, get_type_hints

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


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
    data: dict[str, JsonValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        return {"type": self.type, "data": self.data}


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
    data: JsonValue = None
    error: ToolError | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    client_events: tuple[ClientEvent, ...] = ()
    wait_for_user: bool = False
    timeout_seconds: float | None = None

    @classmethod
    def success(cls, content: str = "", *, data: JsonValue = None) -> "ToolResult":
        return cls(content=content, data=data)

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
    for name, parameter in signature.parameters.items():
        if parameter.kind in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}:
            continue
        if parameter.kind == parameter.KEYWORD_ONLY and parameter.default is not inspect.Parameter.empty:
            continue
        annotation = (type_hints or {}).get(name, parameter.annotation)
        properties[name] = _annotation_schema(annotation)
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _annotation_schema(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Signature.empty:
        return {"type": "string"}
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
    if origin in {dict, tuple, set}:
        return {"type": "object"}
    if origin is not None and type(None) in args:
        return _annotation_schema(next(arg for arg in args if arg is not type(None)))
    return {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
    }.get(annotation, {"type": "string"})


__all__ = [
    "ArtifactRef",
    "ClientEvent",
    "JsonValue",
    "Tool",
    "ToolCall",
    "ToolCallDelta",
    "ToolError",
    "ToolResult",
    "tool_parameters_schema",
    "provider_tool_schema",
]
