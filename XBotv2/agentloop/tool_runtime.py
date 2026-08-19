"""Tool execution for the agent-loop tool service."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from XBotv2.core.events import EventPort, Events, ToolAction, ToolDecision
from XBotv2.core.tools import GuardDecision, ToolCall, ToolError, ToolResult, tool_parameters_schema
from XBotv2.core.messages import Message

logger = logging.getLogger("XBotv2.agentloop.tools")


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
) -> list[Message]:
    """Execute tool calls through the guard pipeline.

    Pipeline per call:
    1. ``BEFORE_TOOL_CALL`` event waterfall (rewrite / deny / stop).
    2. Schema validation.
    3. Registered guards. Guards must resolve their own policy to allow/deny.
    4. Dispatch with dependencies captured when the tool was registered.
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
        tool_name = call.name
        entry = registry.get(tool_name) if registry is not None else None
        logger.info(
            "tool.guard start id=%s name=%s args_keys=%s",
            call.id, tool_name, sorted(call.args),
        )

        if entry is None:
            await _emit_tool_denied(events, context_factory, call, f"Tool not registered: {tool_name}")
            results.append(_error_message(call, f"Tool not registered: {tool_name}"))
            observed_tool_calls.append(call)
            continue

        await _execute_one_tool(
            call, entry, registry,
            events=events,
            guards=guards,
            context_factory=context_factory,
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
        error=error.to_dict() if error is not None else None,
    )


async def _execute_one_tool(
    call: ToolCall, entry: Any, registry: Any,
    *,
    events: Any,
    guards: tuple[Any, ...],
    context_factory: Any,
    results: list[Message], observed_tool_calls: list[ToolCall],
) -> None:
    tool_id = call.id
    tool_name = call.name
    logger.info("tool.execute start id=%s name=%s", tool_id, tool_name)

    tool = entry.tool
    args = dict(call.args)
    before_ctx = (
        context_factory(tool_call=ToolCall(tool_id, tool_name, args))
        if context_factory is not None
        else None
    )
    before_result = (
        await events.serial(Events.BEFORE_TOOL_CALL, before_ctx)
        if events is not None and before_ctx is not None
        else None
    )
    if isinstance(before_result, dict):
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
                results.append(msg)
                await _emit_tool_denied(events, context_factory, call, msg.content)
                return
            tool = entry.tool
            args = dict(call.args)
        if "args" in before_result:
            args = dict(before_result["args"])
        if "tool_result" in before_result:
            message = _coerce_tool_message(before_result["tool_result"], tool_id)
            observed_call = ToolCall(tool_id, tool_name, args)
            observed_tool_calls.append(observed_call)
            results.append(message)
            await _run_tool_event(events, context_factory, Events.AFTER_TOOL_CALL, tool_call=observed_call, tool_result=message, short_circuit=False)
            return
        if "deny_reason" in before_result:
            observed_call = ToolCall(tool_id, tool_name, args)
            msg = _error_message(observed_call, str(before_result["deny_reason"]))
            observed_tool_calls.append(observed_call)
            results.append(msg)
            await _emit_tool_denied(events, context_factory, observed_call, str(before_result["deny_reason"]))
            return
    elif isinstance(before_result, ToolDecision):
        if before_result.action is ToolAction.ALLOW:
            pass
        elif before_result.action is ToolAction.DENY:
            reason = before_result.reason or f"Tool call denied by hook: {tool_name}"
            if before_ctx is not None:
                before_ctx.deny_reason = reason
            observed_call = ToolCall(tool_id, tool_name, args)
            msg = _error_message(observed_call, reason)
            observed_tool_calls.append(observed_call)
            results.append(msg)
            await _emit_tool_denied(events, context_factory, observed_call, reason)
            return
        elif before_result.action is ToolAction.STOP:
            reason = before_result.reason or f"Tool call stopped by hook: {tool_name}"
            if before_ctx is not None:
                before_ctx.deny_reason = reason
            observed_call = ToolCall(tool_id, tool_name, args)
            msg = _error_message(observed_call, reason)
            observed_tool_calls.append(observed_call)
            results.append(msg)
            return
    elif before_result is not None:
        observed_call = ToolCall(tool_id, tool_name, args)
        msg = _error_message(observed_call, f"Tool call blocked by hook: {tool_name}")
        observed_tool_calls.append(observed_call)
        results.append(msg)
        await _emit_tool_denied(events, context_factory, observed_call, str(msg.content))
        return

    call = ToolCall(tool_id, tool_name, args)
    try:
        Draft202012Validator(tool_parameters_schema(tool)).validate(args)
    except ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path)
        location = f" at {path}" if path else ""
        reason = f"Invalid arguments for {tool_name}{location}: {exc.message}"
        observed_tool_calls.append(call)
        results.append(_error_message(call, reason))
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
        await _emit_tool_denied(events, context_factory, call, reason)
        results.append(_error_message(call, reason, events=client_events))
        observed_tool_calls.append(call)
        return
    try:
        result = await _invoke_tool(
            tool,
            args,
            injected={**entry.injected, "tool_call_id": tool_id},
            timeout_seconds=entry.timeout_seconds,
        )

        message = _coerce_tool_message(result, tool_id)
        observed_call = ToolCall(tool_id, tool_name, args)
        observed_tool_calls.append(observed_call)
        results.append(message)
        logger.info("tool.execute finished id=%s name=%s status=%s content_len=%d", tool_id, tool_name, message.status, len(str(message.content)))
        await _run_tool_event(events, context_factory, Events.AFTER_TOOL_CALL, tool_call=observed_call, tool_result=message, short_circuit=False)

    except ToolDispatchTimeoutError as exc:
        logger.warning(
            "Tool %s timed out id=%s timeout=%s",
            tool_name,
            tool_id,
            exc.timeout_seconds,
        )
        observed_call = ToolCall(tool_id, tool_name, args)
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
        results.append(message)
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
        logger.exception("Tool %s failed", tool_name)
        observed_call = ToolCall(tool_id, tool_name, args)
        message = _error_message(observed_call, f"Error executing {tool_name}: {exc}")
        observed_tool_calls.append(observed_call)
        results.append(message)
        await _run_tool_event(events, context_factory, Events.TOOL_CALL_FAILURE, tool_call=observed_call, tool_result=message, error=exc, short_circuit=False)
        await _run_tool_event(events, context_factory, Events.AFTER_TOOL_CALL, tool_call=observed_call, tool_result=message, error=exc, short_circuit=False)


def _coerce_tool_message(value: Any, tool_call_id: str) -> Message:
    if isinstance(value, Message):
        if value.role != "tool":
            raise ValueError("A tool may return only a tool-role Message")
        return value
    if isinstance(value, ToolResult):
        return Message(
            role="tool",
            content=value.content,
            tool_call_id=tool_call_id,
            status=value.status,
            artifact=list(value.artifacts),
            images=list(value.images),
            error=value.error.to_dict() if value.error is not None else None,
            client_events=[
                _normalize_client_event(event.to_dict(), tool_call_id)
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
    injected: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
) -> Any:
    """Invoke any registered tool without blocking the event loop."""
    injected = dict(injected or {})
    if hasattr(tool, "ainvoke"):
        call = tool.ainvoke(args, **injected)
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
