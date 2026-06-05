"""Tool execution node with hook integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from xbotv2.core.interactions import UserInputDisconnected
from xbotv2.hooks.types import HookStage

logger = logging.getLogger("xbotv2.tools.runtime")


async def execute_tools(
    tool_calls: list[dict[str, Any]],
    registry: Any,  # ToolRegistry
    *,
    sandbox_policy: Any = None,  # SandboxPolicy
    permission_system: Any = None,  # PermissionSystem
    hook_manager: Any = None,
    hook_context_factory: Any = None,
    client_interaction_handler: Any = None,
    permission_interaction_handler: Any = None,
    workspace_root: str = "/tmp/xbotv2-workspace",
) -> list[ToolMessage]:
    """Execute tool calls through the guard pipeline.

    Pipeline:
    1. Extract tool calls from the last AIMessage.
    2. Run before_tools hooks (sandbox/permission checks).
    3. Execute approved tools.
    4. Return ToolMessages.

    Args:
        tool_calls: List of {"name": str, "args": dict, "id": str} dicts.
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
        List of ToolMessage instances (one per tool call).
    """
    results: list[ToolMessage] = []
    observed_tool_calls: list[dict[str, Any]] = []
    denials: dict[str, str] = {}  # tool_call_id → reason
    denial_events: dict[str, list[dict[str, Any]]] = {}
    sequential_tools: set[str] = set()

    # Phase 1: Guards — check sandbox and permissions for each tool
    for tc in tool_calls:
        tool_name = tc["name"]
        entry = registry.get(tool_name) if registry else None

        if entry is None:
            denials[tc["id"]] = f"Tool not registered: {tool_name}"
            await _emit_tool_denied(
                hook_manager,
                hook_context_factory,
                tc,
                denials[tc["id"]],
            )
            continue

        # Sandbox guard
        if sandbox_policy and entry.sandbox_mode == "sandboxed":
            allowed, reason = sandbox_policy.guard_tool_call(
                tool_name, tc.get("args", {}), entry.sandbox_mode
            )
            if not allowed:
                denials[tc["id"]] = reason
                permission_stage = (
                    HookStage.ON_PERMISSION_REQUEST
                    if "approval required" in reason.lower()
                    else HookStage.ON_PERMISSION_DENIED
                )
                denial_events[tc["id"]] = [_permission_client_event(
                    permission_stage,
                    tc,
                    "ask" if permission_stage == HookStage.ON_PERMISSION_REQUEST else "deny",
                    reason,
                    source="sandbox",
                )]
                await _emit_permission_event(
                    hook_manager,
                    hook_context_factory,
                    permission_stage,
                    tc,
                    "ask" if permission_stage == HookStage.ON_PERMISSION_REQUEST else "deny",
                    reason,
                )
                if permission_stage == HookStage.ON_PERMISSION_REQUEST:
                    response = await _resolve_live_permission(
                        denial_events[tc["id"]][0],
                        permission_interaction_handler,
                        tc,
                    )
                    if response.get("decision") == "allow":
                        denials.pop(tc["id"], None)
                        denial_events.pop(tc["id"], None)
                        _approve_sandbox_once(sandbox_policy, tc)
                        if entry.execution_mode == "sequential":
                            sequential_tools.add(tool_name)
                        continue
                    denials[tc["id"]] = _permission_denial_reason(response, reason)
                    await _emit_tool_denied(
                        hook_manager,
                        hook_context_factory,
                        tc,
                        denials[tc["id"]],
                    )
                    continue
                await _emit_tool_denied(hook_manager, hook_context_factory, tc, reason)
                continue

        # Permission guard
        if permission_system:
            decision = permission_system.check(tool_name, tc.get("args", {}))
            if decision == "deny":
                denials[tc["id"]] = f"Permission denied for tool: {tool_name}"
                denial_events[tc["id"]] = [_permission_client_event(
                    HookStage.ON_PERMISSION_DENIED,
                    tc,
                    decision,
                    denials[tc["id"]],
                )]
                await _emit_permission_event(
                    hook_manager,
                    hook_context_factory,
                    HookStage.ON_PERMISSION_DENIED,
                    tc,
                    decision,
                    denials[tc["id"]],
                )
                await _emit_tool_denied(
                    hook_manager,
                    hook_context_factory,
                    tc,
                    denials[tc["id"]],
                )
                continue
            if decision == "ask":
                denials[tc["id"]] = (
                    f"Permission approval required for tool: {tool_name}. "
                    "No live permission handler is available, so this call "
                    "fails closed."
                )
                denial_events[tc["id"]] = [_permission_client_event(
                    HookStage.ON_PERMISSION_REQUEST,
                    tc,
                    decision,
                    denials[tc["id"]],
                )]
                await _emit_permission_event(
                    hook_manager,
                    hook_context_factory,
                    HookStage.ON_PERMISSION_REQUEST,
                    tc,
                    decision,
                    denials[tc["id"]],
                )
                response = await _resolve_live_permission(
                    denial_events[tc["id"]][0],
                    permission_interaction_handler,
                    tc,
                )
                if response.get("decision") == "allow":
                    denials.pop(tc["id"], None)
                    denial_events.pop(tc["id"], None)
                    if entry.execution_mode == "sequential":
                        sequential_tools.add(tool_name)
                    continue
                denials[tc["id"]] = _permission_denial_reason(
                    response,
                    denials[tc["id"]],
                )
                await _emit_tool_denied(
                    hook_manager,
                    hook_context_factory,
                    tc,
                    denials[tc["id"]],
                )
                continue

        # Track sequential tools
        if entry.execution_mode == "sequential":
            sequential_tools.add(tool_name)

    # Phase 2: Execute approved tools
    for tc in tool_calls:
        tool_id = tc["id"]

        if tool_id in denials:
            observed_tool_calls.append(tc)
            results.append(ToolMessage(
                content=f"Error: {denials[tool_id]}",
                tool_call_id=tool_id,
                status="error",
                additional_kwargs={"xbotv2_events": denial_events.get(tool_id, [])},
            ))
            continue

        tool_name = tc["name"]
        entry = registry.get(tool_name)
        if entry is None:
            continue

        tool = entry.tool
        args = dict(tc.get("args", {}))
        if sandbox_policy and entry.sandbox_mode == "sandboxed":
            args = _resolve_tool_paths(args, sandbox_policy)

        before_result = await _run_tool_hook(
            hook_manager,
            hook_context_factory,
            HookStage.BEFORE_TOOL_CALL,
            tool_call={**tc, "args": args},
            short_circuit=True,
        )
        if isinstance(before_result, dict):
            if "tool_call" in before_result:
                tc = {**tc, **before_result["tool_call"]}
                tool_id = tc.get("id", tool_id)
                tool_name = tc["name"]
                entry = registry.get(tool_name)
                if entry is None:
                    message = ToolMessage(
                        content=f"Error: Tool not registered: {tool_name}",
                        tool_call_id=tool_id,
                        status="error",
                    )
                    observed_tool_calls.append(tc)
                    results.append(message)
                    await _emit_tool_denied(
                        hook_manager,
                        hook_context_factory,
                        tc,
                        f"Tool not registered: {tool_name}",
                    )
                    continue
                tool = entry.tool
                args = dict(tc.get("args", {}))
                if sandbox_policy and entry.sandbox_mode == "sandboxed":
                    args = _resolve_tool_paths(args, sandbox_policy)
            if "args" in before_result:
                args = dict(before_result["args"])
                if sandbox_policy and entry.sandbox_mode == "sandboxed":
                    args = _resolve_tool_paths(args, sandbox_policy)
            if "tool_result" in before_result:
                message = _coerce_tool_message(before_result["tool_result"], tool_id)
                observed_tool_calls.append({**tc, "args": args})
                results.append(message)
                await _run_tool_hook(
                    hook_manager,
                    hook_context_factory,
                    HookStage.AFTER_TOOL_CALL,
                    tool_call={**tc, "args": args},
                    tool_result=message,
                    short_circuit=False,
                )
                continue
            if "deny_reason" in before_result:
                message = ToolMessage(
                    content=f"Error: {before_result['deny_reason']}",
                    tool_call_id=tool_id,
                    status="error",
                )
                observed_tool_calls.append({**tc, "args": args})
                results.append(message)
                await _emit_tool_denied(
                    hook_manager,
                    hook_context_factory,
                    tc,
                    str(before_result["deny_reason"]),
                )
                continue
        elif before_result is not None:
            message = ToolMessage(
                content=f"Error: Tool call blocked by hook: {tool_name}",
                tool_call_id=tool_id,
                status="error",
            )
            observed_tool_calls.append({**tc, "args": args})
            results.append(message)
            await _emit_tool_denied(
                hook_manager,
                hook_context_factory,
                tc,
                f"Tool call blocked by hook: {tool_name}",
            )
            continue

        try:
            # Acquire resource locks for sequential tools
            if tool_name in sequential_tools:
                lock_keys = entry.lock_fields
                if lock_keys:
                    # Simple serialization for sequential tools
                    pass

            # Execute. LangChain StructuredTool.ainvoke can hang for sync
            # tools in some dependency combinations; use invoke for sync
            # tools.
            #
            # IMPORTANT: ``tool.invoke(args)`` is a *synchronous* call
            # — running it directly in this async function would
            # block the entire asyncio event loop for the duration
            # of the tool (e.g. the shell tool's subprocess.run has
            # its own 30s timeout, so a slow command would freeze
            # the SSE stream, the HTTP server, and any other
            # in-flight requests for 30s). Per the user's 2026-06-05
            # "tool chain" review: run sync tools in a worker thread
            # and add a hard wall-clock timeout on top.
            if getattr(tool, "coroutine", None) is not None and hasattr(tool, "ainvoke"):
                result = await _invoke_with_timeout(
                    tool.ainvoke, args,
                    tool_name=tool_name,
                )
            elif hasattr(tool, "invoke"):
                result = await _invoke_with_timeout(
                    _sync_invoke, (tool, args),
                    tool_name=tool_name,
                )
            elif callable(tool):
                result = await _invoke_with_timeout(
                    _sync_callable, (tool, args),
                    tool_name=tool_name,
                )
            else:
                result = f"Tool {tool_name} is not callable"

            if _is_user_input_wait_result(result):
                message = await _tool_message_from_user_input_wait(
                    result,
                    tool_id,
                    client_interaction_handler,
                )
                observed_tool_calls.append({**tc, "args": args})
                results.append(message)
                await _run_tool_hook(
                    hook_manager,
                    hook_context_factory,
                    HookStage.AFTER_TOOL_CALL,
                    tool_call={**tc, "args": args},
                    tool_result=message,
                    short_circuit=False,
                )
                continue

            message_payload = _message_payload_from_result(result, tool_id)
            message = ToolMessage(
                content=message_payload["content"],
                tool_call_id=tool_id,
                status=message_payload["status"],
                additional_kwargs=message_payload["additional_kwargs"],
            )
            observed_tool_calls.append({**tc, "args": args})
            results.append(message)
            await _run_tool_hook(
                hook_manager,
                hook_context_factory,
                HookStage.AFTER_TOOL_CALL,
                tool_call={**tc, "args": args},
                tool_result=message,
                short_circuit=False,
            )

        except UserInputDisconnected:
            raise
        except Exception as exc:
            logger.exception("Tool %s failed", tool_name)
            message = ToolMessage(
                content=f"Error executing {tool_name}: {exc}",
                tool_call_id=tool_id,
                status="error",
            )
            observed_tool_calls.append({**tc, "args": args})
            results.append(message)
            await _run_tool_hook(
                hook_manager,
                hook_context_factory,
                HookStage.ON_TOOL_CALL_FAILURE,
                tool_call={**tc, "args": args},
                tool_result=message,
                error=exc,
                short_circuit=False,
            )
            await _run_tool_hook(
                hook_manager,
                hook_context_factory,
                HookStage.AFTER_TOOL_CALL,
                tool_call={**tc, "args": args},
                tool_result=message,
                error=exc,
                short_circuit=False,
            )

    # Clear one-call sandbox approvals
    if sandbox_policy and hasattr(sandbox_policy, "clear_one_call_approvals"):
        sandbox_policy.clear_one_call_approvals()

    if hook_manager is not None and hook_context_factory is not None:
        ctx = hook_context_factory(
            HookStage.POST_TOOL_BATCH,
            tool_calls=observed_tool_calls,
            tool_results=results,
        )
        await hook_manager.run(HookStage.POST_TOOL_BATCH, ctx, short_circuit=False)

    return results

def has_tool_calls(messages: list[BaseMessage]) -> bool:
    """Check if the last AI message has pending tool calls."""
    if not messages:
        return False
    last = messages[-1]
    if isinstance(last, AIMessage):
        return bool(getattr(last, "tool_calls", None))
    return False


def extract_tool_calls(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """Extract pending tool calls from the last AI message."""
    if not messages:
        return []
    last = messages[-1]
    if isinstance(last, AIMessage):
        calls = getattr(last, "tool_calls", []) or []
        return [
            {
                "name": c.get("name") if isinstance(c, dict) else getattr(c, "name", ""),
                "args": c.get("args", {}) if isinstance(c, dict) else getattr(c, "args", {}),
                "id": c.get("id") if isinstance(c, dict) else getattr(c, "id", f"call_{i}"),
            }
            for i, c in enumerate(calls)
        ]
    return []


def _resolve_tool_paths(args: dict[str, Any], sandbox_policy: Any) -> dict[str, Any]:
    """Rewrite path-like tool args to the sandbox-resolved absolute path."""
    resolved = dict(args)
    path_keys = {"path", "file_path", "source", "target", "dest", "directory", "dir"}
    for key in path_keys:
        value = resolved.get(key)
        if isinstance(value, str):
            resolved[key] = sandbox_policy.resolve_tool_path(value)
    return resolved


async def _emit_tool_denied(
    hook_manager: Any,
    hook_context_factory: Any,
    tool_call: dict[str, Any],
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
    tool_call: dict[str, Any],
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
    tool_call: dict[str, Any],
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
            "request_id": f"permission:{tool_call.get('id', '')}",
            "source": source,
            "tool_call": tool_call,
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
    tool_call: dict[str, Any],
    reason: str,
    *,
    source: str,
) -> str:
    """Return permission text suitable for live clients."""
    tool_name = str(tool_call.get("name") or "tool")
    if source == "sandbox" and reason.startswith("Path approval required"):
        path = reason.rsplit(": ", 1)[-1] if ": " in reason else ""
        if path and path != reason:
            return f"Path approval required for {tool_name}: {path}"
        return f"Path approval required for {tool_name}."
    if "No live permission handler is available" in reason:
        return f"Permission approval required for tool: {tool_name}."
    if "fails closed" in reason:
        return reason.replace(" This call fails closed.", "").replace(" fails closed.", ".")
    return reason


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
    return isinstance(result, dict) and bool(result.get("wait_for_user"))


async def _tool_message_from_user_input_wait(
    result: dict[str, Any],
    tool_call_id: str,
    client_interaction_handler: Any,
) -> ToolMessage:
    events = [
        _normalize_client_event(event, tool_call_id)
        for event in result.get("events", [])
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
        return ToolMessage(
            content="Error: ask_user did not produce a user_input_required event.",
            tool_call_id=tool_call_id,
            status="error",
        )
    if client_interaction_handler is None:
        request_id = str((event.get("data") or {}).get("request_id") or "")
        return ToolMessage(
            content=(
                "No user reply was received because this runtime does not "
                "support live user input."
            ),
            tool_call_id=tool_call_id,
            status="success",
            additional_kwargs={
                "xbotv2_events": events,
                "xbotv2_user_input_result": {
                    "request_id": request_id,
                    "status": "cancelled",
                    "reason": "live_user_input_unsupported",
                },
            },
        )

    response = await client_interaction_handler(
        event,
        timeout_seconds=result.get("timeout_seconds"),
        tool_call_id=tool_call_id,
    )
    status = str(response.get("status") or "")
    if status == "answered":
        answer = response.get("answer", "")
        content = f"User answered: {answer}"
    elif status == "disconnected":
        raise UserInputDisconnected(
            f"Client disconnected while waiting for {response.get('request_id')}"
        )
    elif status == "timeout":
        content = "No user reply was received before the ask_user timeout."
    elif status == "cancelled":
        reason = response.get("reason") or "cancelled"
        content = f"No user reply was received because the request was cancelled: {reason}"
    else:
        content = "No user reply was received."
    return ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
        status="success",
    )


async def _resolve_live_permission(
    event: dict[str, Any],
    permission_interaction_handler: Any,
    tool_call: dict[str, Any],
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
        tool_call_id=str(tool_call.get("id", "")),
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


def _approve_sandbox_once(sandbox_policy: Any, tool_call: dict[str, Any]) -> None:
    if sandbox_policy is None or not hasattr(sandbox_policy, "approve_once"):
        return
    args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
    path_keys = {"path", "file_path", "source", "target", "dest", "directory", "dir"}
    for key in path_keys:
        value = args.get(key)
        if isinstance(value, str):
            sandbox_policy.approve_once(
                sandbox_policy.resolve_tool_path(value),
                str(tool_call.get("name") or ""),
            )


async def _run_tool_hook(
    hook_manager: Any,
    hook_context_factory: Any,
    stage: HookStage,
    *,
    tool_call: dict[str, Any],
    tool_result: ToolMessage | None = None,
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


def _coerce_tool_message(value: Any, tool_call_id: str) -> ToolMessage:
    if isinstance(value, ToolMessage):
        return value
    if isinstance(value, dict):
        additional_kwargs = {}
        if "events" in value:
            additional_kwargs["xbotv2_events"] = [
                _normalize_client_event(event, tool_call_id)
                for event in value["events"]
            ]
        if value.get("turn_complete") is not None:
            additional_kwargs["xbotv2_turn_complete"] = bool(value["turn_complete"])
        return ToolMessage(
            content=str(value.get("content", "")),
            tool_call_id=str(value.get("tool_call_id", tool_call_id)),
            status=value.get("status", "success"),
            additional_kwargs=additional_kwargs,
        )
    return ToolMessage(content=str(value), tool_call_id=tool_call_id, status="success")


def _message_payload_from_result(result: Any, tool_call_id: str) -> dict[str, Any]:
    message = _coerce_tool_message(result, tool_call_id)
    return {
        "content": message.content,
        "status": getattr(message, "status", "success"),
        "additional_kwargs": getattr(message, "additional_kwargs", {}) or {},
    }


# ----------------------------------------------------------------------
# Sync-tool execution helpers (added 2026-06-05 per user tool-chain review)
# ----------------------------------------------------------------------

# Hard wall-clock cap on a single tool invocation. The shell tool
# already enforces a 30s ``subprocess.run(timeout=30)``, but that
# timeout is for the *command*; the wrapping dispatch loop has no
# timeout. A tool that never returns (e.g. an LLM-augmented tool that
# accidentally awaits the event loop, or a sandbox guard that
# deadlocks) would freeze the asyncio loop indefinitely. 60s is
# generous for any reasonable tool and matches the shell's internal
# timeout.
_TOOL_DISPATCH_TIMEOUT_SECONDS = 60.0


async def _invoke_with_timeout(coro_factory, args, *, tool_name: str) -> Any:
    """Run a (possibly sync) tool call in a worker thread with a timeout.

    ``coro_factory`` is either a coroutine function (for async tools
    via ``ainvoke``) or a plain callable returning a coroutine /
    future (for sync tools dispatched via ``asyncio.to_thread``).
    The wrapper enforces :data:`_TOOL_DISPATCH_TIMEOUT_SECONDS` and
    converts timeouts into a clear error message instead of hanging
    the engine.
    """

    async def _runner() -> Any:
        if asyncio.iscoroutinefunction(coro_factory):
            return await coro_factory(*args)
        # Wrap sync work in a thread so it does not block the
        # event loop. ``asyncio.to_thread`` is the canonical way
        # since 3.9.
        return await asyncio.to_thread(coro_factory, *args)

    try:
        return await asyncio.wait_for(
            _runner(),
            timeout=_TOOL_DISPATCH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "tool %s exceeded dispatch timeout %.1fs",
            tool_name,
            _TOOL_DISPATCH_TIMEOUT_SECONDS,
        )
        return (
            f"Error: tool {tool_name!r} exceeded the "
            f"{_TOOL_DISPATCH_TIMEOUT_SECONDS:.0f}s dispatch timeout"
        )


def _sync_invoke(tool: Any, args: dict[str, Any]) -> Any:
    """Bridge for ``LangChain StructuredTool.invoke``."""

    return tool.invoke(args)


def _sync_callable(tool: Any, args: dict[str, Any]) -> Any:
    """Bridge for a plain Python callable tool."""

    return tool(**args) if args else tool()
