"""Tool execution for the agent-loop tool service."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import inspect
import json
import logging
import time
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from XBotv2.agentloop.events import EventPort, Events
from XBotv2.core.tools import (
    GuardDecision,
    ToolCall,
    ToolError,
    ToolResult,
    tool_parameters_schema,
)
from XBotv2.core.messages import Message
from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
from XBotv2.core.timing import TIMING_METADATA_KEY

_DEFAULT_TOOL_LOG = DEFAULT_RUNTIME_LOG.bind("tools")


def _log_tool_finish(
    runtime_log: RuntimeLog,
    started: float,
    *,
    call_id: str,
    name: str,
    status: str,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    runtime_log.log(
        level,
        "tool.execute.finish",
        call_id=call_id,
        name=name,
        status=status,
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
        **fields,
    )


class ToolDispatchTimeoutError(TimeoutError):
    """The outer tool dispatch deadline expired."""

    def __init__(self, *, tool_name: str, timeout_seconds: float) -> None:
        self.tool_name = tool_name
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Tool {tool_name} timed out after {timeout_seconds}s"
        )


async def execute_tools(
    tool_calls: list[ToolCall],
    registry: Any,  # ToolRegistry
    *,
    events: EventPort | None = None,
    guards: tuple[Any, ...] = (),
    context_factory: Any = None,
    runtime_log: RuntimeLog = _DEFAULT_TOOL_LOG,
) -> list[Message]:
    """Execute tool calls through the guard pipeline.

    Pipeline per call:
    1. ``BEFORE_TOOL_CALL`` event waterfall (rewrite only).
    2. Schema validation.
    3. Registered guards. Guards must resolve their own policy to allow/deny.
    4. Dispatch with standard invocation metadata.
    5. ``AFTER_TOOL_CALL``.

    Args:
        tool_calls: Calls produced by the model adapter.
        registry: ToolRegistry instance.
        events: Narrow event dispatcher used by the loop.
        context_factory: callable that builds EventContext objects (optional).

    Returns:
        List of tool messages (one per tool call).
    """
    results: list[Message] = []
    observed_tool_calls: list[ToolCall] = []

    for call in tool_calls:
        started = time.perf_counter()
        tool_name = call.name
        entry = registry.get(tool_name) if registry is not None else None
        runtime_log.debug(
            "tool.guard.start",
            call_id=call.id,
            name=tool_name,
            argument_fields=sorted(call.args),
            guard_count=len(guards),
        )

        if entry is None:
            _log_tool_finish(
                runtime_log,
                started,
                call_id=call.id,
                name=tool_name,
                status="not_registered",
                level=logging.WARNING,
            )
            await _emit_tool_denied(
                events,
                context_factory,
                call,
                f"Tool not registered: {tool_name}",
            )
            _append_timed_result(
                results,
                _error_message(call, f"Tool not registered: {tool_name}"),
                started,
            )
            observed_tool_calls.append(call)
            continue

        await _execute_one_tool(
            call, entry, registry,
            events=events,
            guards=guards,
            context_factory=context_factory,
            runtime_log=runtime_log,
            results=results,
            observed_tool_calls=observed_tool_calls,
        )

    if events is not None and context_factory is not None:
        batch_ctx = context_factory(
            tool_calls=observed_tool_calls,
            tool_results=results,
        )
        await events.emit(Events.POST_TOOL_BATCH, batch_ctx)

    return results


async def _emit_tool_denied(
    events: Any,
    context_factory: Any,
    tool_call: ToolCall,
    reason: str,
) -> None:
    await _run_tool_event(
        events,
        context_factory,
        Events.TOOL_DENIED,
        tool_call=tool_call,
        error=PermissionError(reason),
        short_circuit=False,
    )


def _normalize_client_event(event: dict[str, Any], tool_call_id: str) -> dict[str, Any]:
    """Attach generic correlation metadata to a tool-originated event."""
    if not isinstance(event, dict):
        return event
    normalized = dict(event)
    data = dict(normalized.get("data") or {})
    data.setdefault("tool_call_id", tool_call_id)
    normalized["data"] = data
    return normalized


async def _run_tool_event(
    events: Any,
    context_factory: Any,
    event: str,
    *,
    tool_call: ToolCall,
    tool_result: Message | None = None,
    error: Exception | None = None,
    short_circuit: bool,
) -> Any:
    if events is None or context_factory is None:
        return None
    event_ctx = context_factory(
        tool_call=tool_call,
        tool_result=tool_result,
        error=error,
    )
    if short_circuit:
        return await events.serial(event, event_ctx)
    await events.emit(event, event_ctx)
    return None


def _error_message(
    call: ToolCall,
    reason: str,
    events: list[dict[str, Any]] | None = None,
    error: ToolError | None = None,
) -> Message:
    return Message(
        role="tool",
        content=f"Error: {reason}",
        tool_call_id=call.id,
        status="error",
        client_events=events,
        error=asdict(error) if error is not None else None,
    )


def _append_timed_result(
    results: list[Message],
    message: Message,
    started: float,
) -> None:
    message.response_metadata[TIMING_METADATA_KEY] = {
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
    }
    results.append(message)


async def _execute_one_tool(
    call: ToolCall, entry: Any, registry: Any,
    *,
    events: Any,
    guards: tuple[Any, ...],
    context_factory: Any,
    runtime_log: RuntimeLog,
    results: list[Message], observed_tool_calls: list[ToolCall],
) -> None:
    tool_id = call.id
    tool_name = call.name
    started = time.perf_counter()
    runtime_log.info(
        "tool.execute.start",
        call_id=tool_id,
        name=tool_name,
        argument_fields=sorted(call.args),
    )

    tool = entry.tool
    args = dict(call.args)
    before_ctx = (
        context_factory(tool_call=ToolCall(id=tool_id, name=tool_name, args=args))
        if context_factory is not None
        else None
    )
    before_result = (
        await events.serial(Events.BEFORE_TOOL_CALL, before_ctx)
        if events is not None and before_ctx is not None
        else None
    )
    if before_result is not None:
        if not isinstance(before_result, dict):
            raise TypeError(
                "BEFORE_TOOL_CALL must return a tool_call/args rewrite or None"
            )
        unsupported = set(before_result) - {"tool_call", "args"}
        if unsupported:
            fields = ", ".join(sorted(unsupported))
            raise TypeError(
                f"BEFORE_TOOL_CALL cannot short-circuit tool policy: {fields}"
            )
        if "tool_call" in before_result:
            call = before_result["tool_call"]
            if not isinstance(call, ToolCall):
                raise TypeError("BEFORE_TOOL_CALL tool_call must be a ToolCall")
            tool_id = call.id
            tool_name = call.name
            entry = registry.get(tool_name)
            if entry is None:
                msg = _error_message(call, f"Tool not registered: {tool_name}")
                observed_tool_calls.append(call)
                _append_timed_result(results, msg, started)
                _log_tool_finish(
                    runtime_log,
                    started,
                    call_id=tool_id,
                    name=tool_name,
                    status="not_registered_after_rewrite",
                    level=logging.WARNING,
                )
                await _emit_tool_denied(events, context_factory, call, msg.content)
                return
            tool = entry.tool
            args = dict(call.args)
        if "args" in before_result:
            rewritten_args = before_result["args"]
            if not isinstance(rewritten_args, dict):
                raise TypeError("BEFORE_TOOL_CALL args must be a dict")
            args = dict(rewritten_args)

    call = ToolCall(id=tool_id, name=tool_name, args=args)
    try:
        Draft202012Validator(tool_parameters_schema(tool)).validate(args)
    except ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path)
        location = f" at {path}" if path else ""
        reason = f"Invalid arguments for {tool_name}{location}: {exc.message}"
        observed_tool_calls.append(call)
        _append_timed_result(results, _error_message(call, reason), started)
        _log_tool_finish(
            runtime_log,
            started,
            call_id=tool_id,
            name=tool_name,
            status="invalid_arguments",
            level=logging.WARNING,
            invalid_path=path,
        )
        await _emit_tool_denied(
            events,
            context_factory,
            call,
            reason,
        )
        return

    # Guards own their policy dependencies. The executor only combines their
    # final decisions; it never discovers or invokes another plugin service.
    denial: GuardDecision | None = None
    for guard in guards:
        decision = guard(call, entry)
        if inspect.isawaitable(decision):
            decision = await decision
        if decision is None:
            continue
        if not isinstance(decision, GuardDecision):
            raise TypeError("tool guards must return GuardDecision or None")
        denial = decision
        break
    if denial is not None:
        reason = denial.reason or f"Tool denied: {tool_name}"
        client_events = list(denial.client_events)
        _log_tool_finish(
            runtime_log,
            started,
            call_id=tool_id,
            name=tool_name,
            status="denied",
            level=logging.WARNING,
        )
        await _emit_tool_denied(events, context_factory, call, reason)
        _append_timed_result(
            results,
            _error_message(call, reason, events=client_events),
            started,
        )
        observed_tool_calls.append(call)
        return
    try:
        result = await _invoke_tool(
            tool,
            args,
            tool_call=call,
            timeout_seconds=entry.timeout_seconds,
        )

        message = _coerce_tool_message(result, tool_id)
        observed_call = ToolCall(id=tool_id, name=tool_name, args=args)
        observed_tool_calls.append(observed_call)
        _append_timed_result(results, message, started)
        _log_tool_finish(
            runtime_log,
            started,
            call_id=tool_id,
            name=tool_name,
            status=message.status,
            result_chars=len(str(message.content)),
        )
        await _run_tool_event(
            events,
            context_factory,
            Events.AFTER_TOOL_CALL,
            tool_call=observed_call,
            tool_result=message,
            short_circuit=False,
        )

    except ToolDispatchTimeoutError as exc:
        _log_tool_finish(
            runtime_log,
            started,
            call_id=tool_id,
            name=tool_name,
            status="timeout",
            level=logging.WARNING,
            timeout_seconds=exc.timeout_seconds,
        )
        observed_call = ToolCall(id=tool_id, name=tool_name, args=args)
        timeout = exc.timeout_seconds
        reason = f"Tool {tool_name} timed out after {timeout}s"
        message = _error_message(
            observed_call,
            reason,
            error=ToolError(
                code="tool_timeout",
                message=reason,
                retryable=False,
                details={"timeout_seconds": timeout},
            ),
        )
        observed_tool_calls.append(observed_call)
        _append_timed_result(results, message, started)
        await _run_tool_event(
            events,
            context_factory,
            Events.TOOL_CALL_FAILURE,
            tool_call=observed_call,
            tool_result=message,
            error=exc,
            short_circuit=False,
        )
        await _run_tool_event(
            events,
            context_factory,
            Events.AFTER_TOOL_CALL,
            tool_call=observed_call,
            tool_result=message,
            error=exc,
            short_circuit=False,
        )
    except Exception as exc:
        _log_tool_finish(
            runtime_log,
            started,
            call_id=tool_id,
            name=tool_name,
            status="error",
            level=logging.ERROR,
            error_type=type(exc).__name__,
        )
        observed_call = ToolCall(id=tool_id, name=tool_name, args=args)
        message = _error_message(observed_call, f"Error executing {tool_name}: {exc}")
        observed_tool_calls.append(observed_call)
        _append_timed_result(results, message, started)
        await _run_tool_event(
            events,
            context_factory,
            Events.TOOL_CALL_FAILURE,
            tool_call=observed_call,
            tool_result=message,
            error=exc,
            short_circuit=False,
        )
        await _run_tool_event(
            events,
            context_factory,
            Events.AFTER_TOOL_CALL,
            tool_call=observed_call,
            tool_result=message,
            error=exc,
            short_circuit=False,
        )


def _coerce_tool_message(value: Any, tool_call_id: str) -> Message:
    if isinstance(value, Message):
        if value.role != "tool":
            raise ValueError("A tool may return only a tool-role Message")
        return value
    if isinstance(value, ToolResult):
        return Message(
            role="tool",
            content=value.content,
            data=value.data,
            tool_call_id=tool_call_id,
            status=value.status,
            artifact=list(value.artifacts),
            images=list(value.images),
            error=asdict(value.error) if value.error is not None else None,
            client_events=[
                _normalize_client_event(asdict(event), tool_call_id)
                for event in value.client_events
            ],
            turn_complete=value.turn_complete,
        )
    if value is None:
        content = ""
    elif isinstance(value, str):
        content = value
    else:
        content = json.dumps(value, ensure_ascii=False, default=str)
    return Message(
        role="tool",
        content=content,
        tool_call_id=tool_call_id,
        status="success",
    )


async def _invoke_tool(
    tool: Any,
    args: dict[str, Any],
    *,
    tool_call: ToolCall,
    timeout_seconds: float | None = None,
) -> Any:
    """Invoke any registered tool without blocking the event loop."""
    if hasattr(tool, "ainvoke"):
        call = tool.ainvoke(args, tool_call=tool_call)
    elif hasattr(tool, "invoke"):
        call = asyncio.to_thread(tool.invoke, args)
    elif callable(tool):
        call = asyncio.to_thread(tool, **args)
    else:
        raise TypeError(f"Tool {tool!r} is not callable")
    task = asyncio.create_task(call)
    if timeout_seconds is None:
        return await task
    done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
    if not done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise ToolDispatchTimeoutError(
            tool_name=getattr(tool, "name", str(tool)),
            timeout_seconds=timeout_seconds,
        )
    return task.result()
