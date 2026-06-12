"""XBot-owned tool protocol."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, get_args, get_origin


@dataclass(frozen=True)
class XBotTool:
    name: str
    description: str
    function: Callable[..., Any]
    parameters: dict[str, Any]
    _extra_param_names: tuple[str, ...] = ()

    @classmethod
    def from_function(cls, function: Callable[..., Any], *, name: str | None = None) -> "XBotTool":
        signature = inspect.signature(function)
        extra = tuple(
            name for name, p in signature.parameters.items()
            if p.kind == p.KEYWORD_ONLY and p.default is not inspect.Parameter.empty
        )
        return cls(
            name=name or function.__name__,
            description=(inspect.getdoc(function) or "").strip(),
            function=function,
            parameters=parameters_schema(signature),
            _extra_param_names=extra,
        )

    def invoke(self, args: dict[str, Any], **extra: Any) -> Any:
        filtered = {k: v for k, v in extra.items() if k in self._extra_param_names}
        result = self.function(**args, **filtered)
        if inspect.isawaitable(result):
            import asyncio
            return asyncio.run(result)
        return result

    async def ainvoke(self, args: dict[str, Any], **extra: Any) -> Any:
        filtered = {k: v for k, v in extra.items() if k in self._extra_param_names}
        result = self.function(**args, **filtered)
        if inspect.isawaitable(result):
            return await result
        return result

    def to_provider_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def provider_tool_schema(tool: Any) -> Any:
    if isinstance(tool, XBotTool):
        return tool.to_provider_schema()
    if hasattr(tool, "to_provider_schema"):
        return tool.to_provider_schema()
    return tool


def parameters_schema(signature: inspect.Signature) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, parameter in signature.parameters.items():
        if parameter.kind in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}:
            continue
        if parameter.kind == parameter.KEYWORD_ONLY and parameter.default is not inspect.Parameter.empty:
            continue
        properties[name] = json_schema_for_annotation(parameter.annotation)
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def json_schema_for_annotation(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Signature.empty:
        return {"type": "string"}
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is list:
        item_annotation = args[0] if args else str
        return {"type": "array", "items": json_schema_for_annotation(item_annotation)}
    if origin in {dict, tuple, set}:
        return {"type": "object"}
    if origin is not None and type(None) in args:
        non_none = next((arg for arg in args if arg is not type(None)), str)
        return json_schema_for_annotation(non_none)
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    return {"type": "string"}
