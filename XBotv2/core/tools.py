"""Tool definition, invocation, and result API."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, TypeAlias, get_args, get_origin, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from XBotv2.core.artifacts import ArtifactRef
from XBotv2.core.domain import StructuredToolOutput, ToolCallId, ToolTiming
from XBotv2.core.parts import ImagePart, TextPart


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
    pass


class ToolCall(BaseModel):
    id: ToolCallId = Field(min_length=1)
    name: str = Field(min_length=1)
    args: dict[str, JsonValue] = Field(default_factory=dict)
    type: Literal["tool_call"] = "tool_call"
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: Mapping[str, JsonValue] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolCallRef(BaseModel):
    id: ToolCallId
    name: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolOutput(BaseModel):
    parts: tuple[TextPart | ImagePart, ...] = ()
    structured: StructuredToolOutput | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)


class ToolSucceeded(BaseModel):
    kind: Literal["succeeded"] = "succeeded"
    output: ToolOutput
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolFailed(BaseModel):
    kind: Literal["failed"] = "failed"
    error: ToolError
    output: ToolOutput
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolDenied(BaseModel):
    kind: Literal["denied"] = "denied"
    reason: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolCancelled(BaseModel):
    kind: Literal["cancelled"] = "cancelled"
    reason: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


ToolOutcome: TypeAlias = ToolSucceeded | ToolFailed | ToolDenied | ToolCancelled


def text_output(text: str, *, artifacts: tuple[ArtifactRef, ...] = ()) -> ToolOutput:
    """Create the canonical textual tool output."""
    return ToolOutput(parts=(TextPart(text=text),), artifacts=artifacts)


def succeeded_text(text: str, *, artifacts: tuple[ArtifactRef, ...] = ()) -> ToolSucceeded:
    return ToolSucceeded(output=text_output(text, artifacts=artifacts))


def failed_text(code: str, message: str, *, retryable: bool = False) -> ToolFailed:
    return ToolFailed(
        error=ToolError(code=code, message=message, retryable=retryable),
        output=ToolOutput(),
    )


class ContinueTurn(BaseModel):
    kind: Literal["continue"] = "continue"
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompleteTurn(BaseModel):
    kind: Literal["complete"] = "complete"
    model_config = ConfigDict(extra="forbid", frozen=True)


TurnDirective: TypeAlias = ContinueTurn | CompleteTurn


class ToolExecution(BaseModel):
    message: "ToolMessage"
    events: tuple[object, ...] = ()
    directive: TurnDirective
    model_config = ConfigDict(extra="forbid", frozen=True)

@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    function: Callable[..., Any]
    parameters: dict[str, Any]
    tool_call_parameter: str | None = None
    #: Declared by the owning package: this tool requests execution outside
    #: the sandbox when its arguments ask for escalation. The tool itself
    #: refuses to discharge escalation without an active approval layer, and
    #: the execution pipeline requires an approval-capable guard for these
    #: calls — the sandbox and permission layers read this declaration instead
    #: of hard-coding tool names.
    escapes_sandbox: bool = False
    #: Declared by the owning package: the model-facing category of this tool
    #: (``read``/``edit``/``execute``/``search``/``fetch``/``think``/``other``).
    #: Clients such as ACP render it directly; carriers consume the
    #: declaration instead of re-deriving a taxonomy from tool names.
    kind: str = "other"
    #: Declared by the owning package: the argument names that define this
    #: tool's authorization scope when a session grant is minted from one
    #: call (selectors, not bulk payloads). Empty means "constrain every
    #: scalar argument", which is the conservative fallback.
    grant_selectors: tuple[str, ...] = ()

    @classmethod
    def from_function(
        cls,
        function: Callable[..., Any],
        *,
        name: str | None = None,
        excluded_parameters: frozenset[str] = frozenset(),
    ) -> "Tool":
        signature = inspect.signature(function)
        description = (inspect.getdoc(function) or "").strip()
        try:
            type_hints = get_type_hints(function)
        except (NameError, TypeError):
            type_hints = {}
        tool_call_parameters = tuple(
            parameter_name
            for parameter_name, parameter in signature.parameters.items()
            if type_hints.get(parameter_name, parameter.annotation)
            is ToolCall
        )
        if len(tool_call_parameters) > 1:
            raise TypeError("a Tool may declare only one ToolCall parameter")
        if tool_call_parameters:
            parameter = signature.parameters[tool_call_parameters[0]]
            if parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
                raise TypeError("a ToolCall parameter must be keyword-only")
        return cls(
            name=name or function.__name__,
            description=description,
            function=function,
            parameters=_parameters_schema(
                signature,
                type_hints,
                excluded=frozenset(tool_call_parameters) | excluded_parameters,
            ),
            tool_call_parameter=(
                tool_call_parameters[0] if tool_call_parameters else None
            ),
        )

    def invoke(
        self,
        args: dict[str, Any],
        *,
        tool_call: ToolCall | None = None,
    ) -> Any:
        result = self.function(**args, **self._tool_call(tool_call))
        if inspect.isawaitable(result):
            import asyncio

            return asyncio.run(result)
        return result

    async def ainvoke(
        self,
        args: dict[str, Any],
        *,
        tool_call: ToolCall | None = None,
    ) -> Any:
        kwargs = {**args, **self._tool_call(tool_call)}
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

    def _tool_call(
        self,
        tool_call: ToolCall | None,
    ) -> dict[str, ToolCall]:
        if self.tool_call_parameter is None:
            return {}
        if tool_call is None:
            raise TypeError(f"Tool {self.name!r} requires its ToolCall")
        return {self.tool_call_parameter: tool_call}


def provider_tool_schema(tool: Tool) -> dict[str, Any]:
    return tool.provider_schema()


def tool_parameters_schema(tool: Tool) -> dict[str, Any]:
    """Return the JSON Schema declared by one registered XBot ``Tool``."""
    return tool.parameters


def _parameters_schema(
    signature: inspect.Signature,
    type_hints: dict[str, Any] | None = None,
    *,
    excluded: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    accepts_extra = False
    for name, parameter in signature.parameters.items():
        if name in excluded:
            continue
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
    "CompleteTurn",
    "ContinueTurn",
    "GuardDecision",
    "ToolCallRef",
    "ToolExecution",
    "ToolFailed",
    "ToolOutput",
    "ToolOutcome",
    "ToolSucceeded",
    "ToolDenied",
    "ToolCancelled",
    "TurnDirective",
    "Tool",
    "ToolCall",
    "ToolError",
    "ToolCallRef",
    "ToolCancelled",
    "ToolDenied",
    "ToolFailed",
    "ToolOutput",
    "ToolOutcome",
    "ToolSucceeded",
    "TurnDirective",
    "failed_text",
    "succeeded_text",
    "text_output",
    "tool_parameters_schema",
    "provider_tool_schema",
]
