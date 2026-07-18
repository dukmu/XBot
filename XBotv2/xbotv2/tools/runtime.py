"""Tool execution node with hook integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from xbotv2.api.hooks import HookAction, HookDecision, HookStage
from xbotv2.api.tools import ToolCall, ToolResult, tool_parameters_schema
from xbotv2.core.interactions import UserInputDisconnected
from xbotv2.api.messages import Message

logger = logging.getLogger("xbotv2.tools.runtime")


async def execute_tools(
    tool_calls: list[ToolCall],
    registry: Any,  # ToolRegistry
    *,
    sandbox_policy: Any = None,  # SandboxPolicy
    permission_system: Any = None,  # PermissionSystem
    hook_manager: Any = None,
    hook_context_factory: Any = None,
    client_interaction_handler: Any = None,
    permission_interaction_handler: Any = None,
    workspace_root: str = "/tmp/xbotv2-workspace",
) -> list[Message]:
    """Execute tool calls through the guard pipeline.

    Pipeline:
    1. Extract tool calls from the last assistant message.
    2. Run per-call hooks and apply transformations.
    3. Check core permissions for the final call.
    4. Execute approved tools with their registered sandbox mode.
    5. Return tool messages.

    Args:
        tool_calls: Calls produced by the model adapter.
        registry: ToolRegistry instance.
        sandbox_policy: SandboxPolicy instance (optional).
        permission_system: PermissionSystem instance (optional).
        hook_manager: HookManager instance (optional).
        hook_context_factory: callable that builds HookContext objects.
        client_interaction_handler: async callable for blocking interaction
            events such as ask_user.
        permission_interaction_handler: async callable for live permission
            approvals.
        workspace_root: Workspace root for path resolution.

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
            await _emit_tool_denied(hook_manager, hook_context_factory, call, f"Tool not registered: {tool_name}")
            results.append(_error_message(call, f"Tool not registered: {tool_name}"))
            observed_tool_calls.append(call)
            continue

        await _execute_one_tool(
            call, entry, registry,
            sandbox_policy, permission_system,
            hook_manager, hook_context_factory,
            client_interaction_handler, permission_interaction_handler,
            workspace_root,
            results, observed_tool_calls,
        )

    if hook_manager is not None and hook_context_factory is not None:
        ctx = hook_context_factory(
            HookStage.POST_TOOL_BATCH,
            tool_calls=observed_tool_calls,
            tool_results=results,
        )
        await hook_manager.run(HookStage.POST_TOOL_BATCH, ctx, short_circuit=False)

    return results


async def _emit_tool_denied(
    hook_manager: Any,
    hook_context_factory: Any,
    tool_call: ToolCall,
    reason: str,
) -> None:
    await _run_tool_hook(
        hook_manager,
        hook_context_factory,
        HookStage.ON_TOOL_DENIED,
        tool_call=tool_call,
        error=PermissionError(reason),
        short_circuit=False,
    )


async def _emit_permission_event(
    hook_manager: Any,
    hook_context_factory: Any,
    stage: HookStage,
    tool_call: ToolCall,
    decision: str,
    reason: str,
) -> None:
    if hook_manager is None or hook_context_factory is None:
        return
    ctx = hook_context_factory(
        stage,
        tool_call=tool_call,
        permission_decision=decision,
        error=PermissionError(reason),
    )
    await hook_manager.run(stage, ctx, short_circuit=False)


def _permission_client_event(
    stage: HookStage,
    tool_call: ToolCall,
    decision: str,
    reason: str,
    *,
    source: str = "permission_system",
) -> dict[str, Any]:
    event_type = (
        "permission_request"
        if stage == HookStage.ON_PERMISSION_REQUEST
        else "permission_denied"
    )
    return {
        "type": event_type,
        "data": {
            "request_id": f"permission:{tool_call.id}",
            "source": source,
            "tool_call": tool_call.to_dict(),
            "decision": decision,
            "reason": _client_visible_permission_reason(
                tool_call,
                reason,
                source=source,
            ),
            "resume_supported": False,
        },
    }


def _client_visible_permission_reason(
    tool_call: ToolCall,
    reason: str,
    *,
    source: str,
) -> str:
    """Return a short permission label for fallback / non-TUI clients.

    The Textual TUI ignores this field entirely — it builds the
    widget title from ``tool_call.name`` and ``tool.status``.
    """

    tool_name = tool_call.name or "tool"
    if source == "sandbox" and reason.startswith("Path approval required"):
        path = reason.rsplit(": ", 1)[-1] if ": " in reason else ""
        return f"Path approval for {tool_name}: {path}" if path else f"Path approval for {tool_name}"
    return f"Approval: {tool_name}"


def _normalize_client_event(event: dict[str, Any], tool_call_id: str) -> dict[str, Any]:
    """Attach stable request metadata to tool-originated client events."""
    if not isinstance(event, dict):
        return event
    normalized = dict(event)
    data = dict(normalized.get("data") or {})
    event_type = normalized.get("type", "client_event")

    if event_type == "user_input_required":
        if not data.get("request_id") or data.get("request_id") == "user_input":
            data["request_id"] = f"user_input:{tool_call_id}"
        data.setdefault("source", "ask_user")
        data.setdefault("tool_call_id", tool_call_id)
        data.setdefault("resume_supported", False)
    elif event_type == "client_message":
        data.setdefault("source", "send_message")
        data.setdefault("tool_call_id", tool_call_id)

    normalized["data"] = data
    return normalized


def _is_user_input_wait_result(result: Any) -> bool:
    if isinstance(result, ToolResult):
        return result.wait_for_user
    return isinstance(result, dict) and bool(result.get("wait_for_user"))


async def _tool_message_from_user_input_wait(
    result: ToolResult | dict[str, Any],
    tool_call_id: str,
    client_interaction_handler: Any,
) -> Message:
    raw_events = (
        [event.to_dict() for event in result.client_events]
        if isinstance(result, ToolResult)
        else result.get("events", [])
    )
    events = [
        _normalize_client_event(event, tool_call_id)
        for event in raw_events
    ]
    event = next(
        (
            item
            for item in events
            if isinstance(item, dict) and item.get("type") == "user_input_required"
        ),
        None,
    )
    if event is None:
        return Message(
            role="tool",
            content="Error: ask_user did not produce a user_input_required event.",
            tool_call_id=tool_call_id,
            status="error",
        )
    if client_interaction_handler is None:
        request_id = str((event.get("data") or {}).get("request_id") or "")
        return Message(
            role="tool",
            content=(
                "No user reply was received because this runtime does not "
                "support live user input."
            ),
            tool_call_id=tool_call_id,
            status="error",
            additional_kwargs={
                "xbotv2_events": events,
                "xbotv2_user_input_result": {
                    "request_id": request_id,
                    "status": "cancelled",
                    "reason": "live_user_input_unsupported",
                },
            },
        )

    timeout_seconds = (
        result.timeout_seconds
        if isinstance(result, ToolResult)
        else result.get("timeout_seconds")
    )
    response = await client_interaction_handler(
        event,
        timeout_seconds=timeout_seconds,
        tool_call_id=tool_call_id,
    )
    status = str(response.get("status") or "")
    tool_status = "error"
    if status == "answered":
        answer = response.get("answer", "")
        if isinstance(answer, str) and not answer.strip():
            content = "The user submitted an empty answer."
        else:
            content = f"User answered: {answer}"
            tool_status = "success"
    elif status == "disconnected":
        raise UserInputDisconnected(
            f"Client disconnected while waiting for {response.get('request_id')}"
        )
    elif status == "timeout":
        content = "No user reply was received before the ask_user timeout."
    elif status == "cancelled":
        reason = response.get("reason") or "cancelled"
        content = f"No user reply was received because the request was cancelled: {reason}"
        tool_status = "cancelled"
    else:
        content = "No user reply was received."
    return Message(
        role="tool",
        content=content,
        tool_call_id=tool_call_id,
        status=tool_status,
    )


async def _resolve_live_permission(
    event: dict[str, Any],
    permission_interaction_handler: Any,
    tool_call: ToolCall,
) -> dict[str, Any]:
    if permission_interaction_handler is None:
        return {
            "status": "unsupported",
            "decision": "deny",
            "reason": "live_permission_unsupported",
        }
    response = await permission_interaction_handler(
        event,
        timeout_seconds=None,
        tool_call_id=tool_call.id,
    )
    decision = str(response.get("decision") or "").lower()
    if response.get("status") == "answered" and decision == "allow":
        return {**response, "decision": "allow"}
    if response.get("status") == "answered" and decision == "deny":
        return {**response, "decision": "deny", "reason": "denied_by_user"}
    return {**response, "decision": "deny"}


def _permission_denial_reason(response: dict[str, Any], fallback: str) -> str:
    status = str(response.get("status") or "")
    reason = str(response.get("reason") or status)
    if status == "timeout":
        return "Permission request timed out; tool call denied."
    if status == "answered" and response.get("decision") == "deny":
        return "Permission denied by user."
    if status == "cancelled":
        return f"Permission request cancelled; tool call denied: {reason}"
    if status == "unsupported":
        return fallback
    return fallback


async def _authorize_sandbox_tool(
    call: ToolCall,
    sandbox_policy: Any,
    permission_interaction_handler: Any,
    hook_manager: Any,
    hook_context_factory: Any,
) -> tuple[bool, list[dict[str, Any]], str]:
    issues = sandbox_policy.check_tool_access(call.name, call.args)
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, bool]] = set()
    for issue in issues:
        path = str(issue["path"])
        write = bool(issue["write"])
        key = (path, write)
        if key in seen:
            continue
        seen.add(key)
        decision = str(issue["decision"])
        access = "readwrite" if write else "readonly"
        action = "write" if write else "read"
        reason = (
            f"Path approval required for {action}: {path}"
            if decision == "ask"
            else f"Sandbox denied {action} access: {path}"
        )
        stage = (
            HookStage.ON_PERMISSION_REQUEST
            if decision == "ask"
            else HookStage.ON_PERMISSION_DENIED
        )
        event = _permission_client_event(
            stage,
            call,
            decision,
            reason,
            source="sandbox",
        )
        event["data"].update({
            "sandbox_path": path,
            "sandbox_access": access,
        })
        events.append(event)
        await _emit_permission_event(
            hook_manager,
            hook_context_factory,
            stage,
            call,
            decision,
            reason,
        )
        if decision != "ask":
            return False, events, reason
        response = await _resolve_live_permission(
            event,
            permission_interaction_handler,
            call,
        )
        if response.get("decision") != "allow":
            return False, events, _permission_denial_reason(response, reason)
        sandbox_policy.add_rule(path, access)
    return True, events, ""


async def _run_tool_hook(
    hook_manager: Any,
    hook_context_factory: Any,
    stage: HookStage,
    *,
    tool_call: ToolCall,
    tool_result: Message | None = None,
    error: Exception | None = None,
    short_circuit: bool,
) -> Any:
    if hook_manager is None or hook_context_factory is None:
        return None
    ctx = hook_context_factory(
        stage,
        tool_call=tool_call,
        tool_result=tool_result,
        error=error,
    )
    return await hook_manager.run(stage, ctx, short_circuit=short_circuit)


def _error_message(call: ToolCall, reason: str, events: list[dict[str, Any]] | None = None) -> Message:
    return Message(
        role="tool", content=f"Error: {reason}", tool_call_id=call.id, status="error",
        additional_kwargs={"xbotv2_events": events} if events else {},
    )


async def _execute_one_tool(
    call: ToolCall, entry: Any, registry: Any,
    sandbox_policy: Any, permission_system: Any,
    hook_manager: Any, hook_context_factory: Any,
    client_interaction_handler: Any, permission_interaction_handler: Any,
    workspace_root: str | None,
    results: list[Message], observed_tool_calls: list[ToolCall],
) -> None:
    tool_id = call.id
    tool_name = call.name
    logger.info("tool.execute start id=%s name=%s", tool_id, tool_name)

    tool = entry.tool
    args = dict(call.args)
    if tool_name == "shell" and workspace_root:
        args.setdefault("cwd", workspace_root)

    before_result = await _run_tool_hook(
        hook_manager, hook_context_factory,
        HookStage.BEFORE_TOOL_CALL,
        tool_call=ToolCall(tool_id, tool_name, args),
        short_circuit=True,
    )
    hook_allowed = False
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
                await _emit_tool_denied(hook_manager, hook_context_factory, call, msg.content)
                return
            tool = entry.tool
            args = dict(call.args)
            if tool_name == "shell" and workspace_root:
                args.setdefault("cwd", workspace_root)
        if "args" in before_result:
            args = dict(before_result["args"])
            if tool_name == "shell" and workspace_root:
                args.setdefault("cwd", workspace_root)
        if "tool_result" in before_result:
            message = _coerce_tool_message(before_result["tool_result"], tool_id)
            observed_call = ToolCall(tool_id, tool_name, args)
            observed_tool_calls.append(observed_call)
            results.append(message)
            await _run_tool_hook(hook_manager, hook_context_factory, HookStage.AFTER_TOOL_CALL, tool_call=observed_call, tool_result=message, short_circuit=False)
            return
        if "deny_reason" in before_result:
            observed_call = ToolCall(tool_id, tool_name, args)
            msg = _error_message(observed_call, str(before_result["deny_reason"]))
            observed_tool_calls.append(observed_call)
            results.append(msg)
            await _emit_tool_denied(hook_manager, hook_context_factory, observed_call, str(before_result["deny_reason"]))
            return
    elif isinstance(before_result, HookDecision):
        if before_result.action is HookAction.ALLOW:
            hook_allowed = True
        elif before_result.action is HookAction.DENY:
            reason = before_result.reason or f"Tool call denied by hook: {tool_name}"
            observed_call = ToolCall(tool_id, tool_name, args)
            msg = _error_message(observed_call, reason)
            observed_tool_calls.append(observed_call)
            results.append(msg)
            await _emit_tool_denied(hook_manager, hook_context_factory, observed_call, reason)
            return
        if before_result.action is HookAction.STOP:
            reason = before_result.reason or f"Tool call stopped by hook: {tool_name}"
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
        await _emit_tool_denied(hook_manager, hook_context_factory, observed_call, str(msg.content))
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
            hook_manager,
            hook_context_factory,
            call,
            reason,
        )
        return
    if permission_system:
        decision = permission_system.check(tool_name, args)
        if decision == "deny":
            reason = f"Permission denied for tool: {tool_name}"
            events = [_permission_client_event(HookStage.ON_PERMISSION_DENIED, call, decision, reason)]
            await _emit_permission_event(hook_manager, hook_context_factory, HookStage.ON_PERMISSION_DENIED, call, decision, reason)
            await _emit_tool_denied(hook_manager, hook_context_factory, call, reason)
            results.append(_error_message(call, reason, events=events))
            observed_tool_calls.append(call)
            return
        if decision == "ask" and not hook_allowed:
            reason = f"Permission approval required for tool: {tool_name}. No live permission handler is available, so this call fails closed."
            events = [_permission_client_event(HookStage.ON_PERMISSION_REQUEST, call, decision, reason)]
            await _emit_permission_event(hook_manager, hook_context_factory, HookStage.ON_PERMISSION_REQUEST, call, decision, reason)
            response = await _resolve_live_permission(events[0], permission_interaction_handler, call)
            if response.get("decision") != "allow":
                final_reason = _permission_denial_reason(response, reason)
                await _emit_tool_denied(hook_manager, hook_context_factory, call, final_reason)
                results.append(_error_message(call, final_reason, events=events))
                observed_tool_calls.append(call)
                return

    use_sandbox_policy = (
        entry.sandbox_mode == "sandboxed"
        and sandbox_policy is not None
    )
    if use_sandbox_policy and sandbox_policy.enabled:
        allowed, sandbox_events, reason = await _authorize_sandbox_tool(
            call,
            sandbox_policy,
            permission_interaction_handler,
            hook_manager,
            hook_context_factory,
        )
        if not allowed:
            await _emit_tool_denied(
                hook_manager,
                hook_context_factory,
                call,
                reason,
            )
            results.append(_error_message(call, reason, events=sandbox_events))
            observed_tool_calls.append(call)
            return

    try:
        result = await _invoke_tool(
            tool,
            args,
            sandbox=sandbox_policy if use_sandbox_policy else None,
            timeout_seconds=entry.timeout_seconds,
        )

        if _is_user_input_wait_result(result):
            message = await _tool_message_from_user_input_wait(result, tool_id, client_interaction_handler)
            observed_call = ToolCall(tool_id, tool_name, args)
            observed_tool_calls.append(observed_call)
            results.append(message)
            await _run_tool_hook(hook_manager, hook_context_factory, HookStage.AFTER_TOOL_CALL, tool_call=observed_call, tool_result=message, short_circuit=False)
            return

        message = _coerce_tool_message(result, tool_id)
        observed_call = ToolCall(tool_id, tool_name, args)
        observed_tool_calls.append(observed_call)
        results.append(message)
        logger.info("tool.execute finished id=%s name=%s status=%s content_len=%d", tool_id, tool_name, message.status, len(str(message.content)))
        await _run_tool_hook(hook_manager, hook_context_factory, HookStage.AFTER_TOOL_CALL, tool_call=observed_call, tool_result=message, short_circuit=False)

    except UserInputDisconnected:
        raise
    except Exception as exc:
        logger.exception("Tool %s failed", tool_name)
        observed_call = ToolCall(tool_id, tool_name, args)
        message = _error_message(observed_call, f"Error executing {tool_name}: {exc}")
        observed_tool_calls.append(observed_call)
        results.append(message)
        await _run_tool_hook(hook_manager, hook_context_factory, HookStage.ON_TOOL_CALL_FAILURE, tool_call=observed_call, tool_result=message, error=exc, short_circuit=False)
        await _run_tool_hook(hook_manager, hook_context_factory, HookStage.AFTER_TOOL_CALL, tool_call=observed_call, tool_result=message, error=exc, short_circuit=False)


def _coerce_tool_message(value: Any, tool_call_id: str) -> Message:
    if hasattr(value, "role") and value.role == "tool":
        return value
    if isinstance(value, ToolResult):
        additional_kwargs: dict[str, Any] = {}
        if value.client_events:
            additional_kwargs["xbotv2_events"] = [
                _normalize_client_event(
                    event.to_dict(), tool_call_id
                )
                for event in value.client_events
            ]
        if value.data is not None:
            additional_kwargs["xbotv2_data"] = value.data
        if value.error is not None:
            additional_kwargs["xbotv2_error"] = value.error.to_dict()
        return Message(
            role="tool",
            content=value.content,
            tool_call_id=tool_call_id,
            status=value.status,
            additional_kwargs=additional_kwargs,
            artifact=list(value.artifacts),
        )
    if isinstance(value, dict):
        additional_kwargs: dict[str, Any] = {}
        if "events" in value:
            additional_kwargs["xbotv2_events"] = [
                _normalize_client_event(event, tool_call_id)
                for event in value["events"]
            ]
        if value.get("turn_complete") is not None:
            additional_kwargs["xbotv2_turn_complete"] = bool(value["turn_complete"])
        if "data" in value:
            additional_kwargs["xbotv2_data"] = value["data"]
        if value.get("error") is not None:
            error = value["error"]
            if hasattr(error, "to_dict"):
                error = error.to_dict()
            elif isinstance(error, dict):
                error = dict(error)
            else:
                error = {
                    "code": "tool_error",
                    "message": str(error),
                    "retryable": False,
                    "details": {},
                }
            additional_kwargs["xbotv2_error"] = error
        artifacts = value.get("artifacts", value.get("artifact"))
        if artifacts is not None and not isinstance(artifacts, (list, tuple)):
            artifacts = [artifacts]
        return Message(
            role="tool",
            content=str(value.get("content", "")),
            tool_call_id=str(value.get("tool_call_id", tool_call_id)),
            status=value.get("status", "success"),
            additional_kwargs=additional_kwargs,
            artifact=list(artifacts or []),
        )
    return Message(role="tool", content=str(value), tool_call_id=tool_call_id, status="success")


_TOOL_DISPATCH_TIMEOUT_SECONDS = 60.0


async def _invoke_tool(
    tool: Any,
    args: dict[str, Any],
    *,
    sandbox: Any = None,
    timeout_seconds: float | None = None,
) -> Any:
    """Invoke any registered tool without blocking the event loop."""
    if hasattr(tool, "ainvoke"):
        call = tool.ainvoke(args, **({"sandbox": sandbox} if sandbox else {}))
    elif hasattr(tool, "invoke"):
        call = asyncio.to_thread(tool.invoke, args)
    elif callable(tool):
        call = asyncio.to_thread(tool, **args)
    else:
        raise TypeError(f"Tool {tool!r} is not callable")
    return await asyncio.wait_for(
        call,
        timeout=timeout_seconds or _TOOL_DISPATCH_TIMEOUT_SECONDS,
    )
