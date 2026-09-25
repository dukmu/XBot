"""Canonical tool execution pipeline owned by the agent loop."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import JsonValue

from XBotv2.agentloop.contracts import ToolGuard, ToolRegistration
from XBotv2.agentloop.events import (
    AfterToolExecution,
    BeforeToolCall,
    EventPort,
    Events,
    KeepExecution,
    KeepToolCall,
    ReplaceExecution,
    ReplaceToolCall,
    ToolBatchObserved,
)
from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.core.domain import MessageId, ToolTiming
from XBotv2.core.messages import ToolMessage
from XBotv2.core.parts import TextPart
from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
from XBotv2.core.tools import (
    GuardDecision,
    Tool,
    ToolCall,
    ToolCallRef,
    ToolCancelled,
    ToolDenied,
    ToolError,
    ToolFailed,
    ToolOutput,
    ToolExecution,
    ContinueTurn,
    ToolSucceeded,
    ToolOutcome,
    tool_parameters_schema,
)

_DEFAULT_TOOL_LOG = DEFAULT_RUNTIME_LOG.bind("tools")


class ToolDispatchTimeoutError(TimeoutError):
    def __init__(self, *, tool_name: str, timeout_seconds: float) -> None:
        self.tool_name = tool_name
        self.timeout_seconds = timeout_seconds
        super().__init__(f"Tool {tool_name} timed out after {timeout_seconds}s")


async def execute_tools(
    tool_calls: list[ToolCall],
    registry: ToolRegistry,
    *,
    events: EventPort | None = None,
    guards: tuple[ToolGuard, ...] = (),
    runtime_log: RuntimeLog = _DEFAULT_TOOL_LOG,
    approval_layer_active: bool = False,
) -> AsyncIterator[ToolExecution]:
    observed: list[ToolCall] = []
    executions: list[ToolExecution] = []
    for call in tool_calls:
        started = time.perf_counter()
        entry = registry.get(call.name)
        if entry is None:
            outcome: ToolOutcome = ToolFailed(
                error=ToolError(code="not_registered", message=f"Tool not registered: {call.name}"),
                output=ToolOutput(),
            )
        else:
            call, outcome = await _execute_one(call, entry, registry, events, guards,
                                         runtime_log,
                                         approval_layer_active)
        message = ToolMessage(
            id=MessageId(f"tool-{call.id}-{time.time_ns()}"),
            call=ToolCallRef(id=call.id, name=call.name),
            outcome=outcome,
            timing=ToolTiming(duration_ms=round((time.perf_counter() - started) * 1000, 3)),
        )
        observed.append(call)
        execution = ToolExecution(
            message=message,
            events=(),
            directive=ContinueTurn(),
        )
        if events is not None:
            changed = await events.serial(
                Events.AFTER_TOOL_CALL,
                AfterToolExecution(execution),
            )
            if changed is not None:
                if isinstance(changed, KeepExecution):
                    pass
                elif isinstance(changed, ReplaceExecution):
                    execution = changed.execution
                else:
                    raise TypeError(
                        "AFTER_TOOL_CALL must return KeepExecution, "
                        "ReplaceExecution, or None"
                    )
        executions.append(execution)
        yield execution
    if events is not None:
        await events.emit(
            Events.TOOL_BATCH_OBSERVED,
            ToolBatchObserved(tuple(observed), tuple(executions)),
        )


async def _execute_one(
    call: ToolCall,
    entry: ToolRegistration,
    registry: ToolRegistry,
    events: EventPort | None,
    guards: tuple[ToolGuard, ...],
    runtime_log: RuntimeLog,
    approval_layer_active: bool,
) -> tuple[ToolCall, ToolOutcome]:
    tool = entry.tool
    args = dict(call.args)
    if events is not None:
        result = await events.serial(Events.BEFORE_TOOL_CALL, BeforeToolCall(call))
        if result is not None:
            if isinstance(result, KeepToolCall):
                pass
            elif isinstance(result, ReplaceToolCall):
                call = result.call
            else:
                raise TypeError(
                    "BEFORE_TOOL_CALL must return KeepToolCall, "
                    "ReplaceToolCall, or None"
                )
            replacement_entry = registry.get(call.name)
            if replacement_entry is None:
                return call, ToolFailed(
                    error=ToolError(
                        code="not_registered",
                        message=f"Tool not registered: {call.name}",
                    ),
                    output=ToolOutput(),
                )
            entry = replacement_entry
            tool = entry.tool
            args = dict(call.args)
    try:
        Draft202012Validator(tool_parameters_schema(tool)).validate(args)
    except ValidationError as exc:
        return call, ToolFailed(error=ToolError(code="invalid_arguments", message=exc.message), output=ToolOutput())
    if (tool.escapes_sandbox
            and args.get("sandbox_permissions") == "require_escalated"
            and not approval_layer_active):
        return call, ToolDenied(reason="Sandbox escape requires an active approval layer")
    for guard in guards:
        decision = guard(call, entry)
        if inspect.isawaitable(decision):
            decision = await decision
        if decision is not None:
            if not isinstance(decision, GuardDecision):
                raise TypeError("tool guards must return GuardDecision or None")
            return call, ToolDenied(reason=decision.reason or f"Tool denied: {call.name}")
    try:
        value = await _invoke_tool(tool, args, call, entry.timeout_seconds)
        if isinstance(value, (ToolSucceeded, ToolFailed, ToolDenied, ToolCancelled)):
            outcome: ToolOutcome = value
        else:
            outcome = ToolSucceeded(output=_coerce_output(value))
    except ToolDispatchTimeoutError as exc:
        outcome = ToolFailed(error=ToolError(code="tool_timeout", message=str(exc)), output=ToolOutput())
    except Exception as exc:
        runtime_log.exception("tool.execute.failed", error_type=type(exc).__name__, tool=call.name)
        outcome = ToolFailed(error=ToolError(code="tool_error", message=str(exc)), output=ToolOutput())
    return call, outcome


def _coerce_output(value: Any) -> ToolOutput:
    if isinstance(value, ToolOutput):
        return value
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    return ToolOutput(parts=(TextPart(text=text),))


async def _invoke_tool(tool: Tool, args: dict[str, JsonValue], call: ToolCall,
                       timeout_seconds: float | None) -> Any:
    if timeout_seconds is None:
        return await tool.ainvoke(args, tool_call=call)
    task = asyncio.create_task(tool.ainvoke(args, tool_call=call))
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    if not done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise ToolDispatchTimeoutError(tool_name=tool.name, timeout_seconds=timeout_seconds)
    return task.result()


__all__ = ["ToolDispatchTimeoutError", "execute_tools"]
