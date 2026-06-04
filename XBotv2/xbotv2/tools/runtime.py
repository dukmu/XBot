"""Tool execution node with hook integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

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
                )]
                await _emit_permission_event(
                    hook_manager,
                    hook_context_factory,
                    permission_stage,
                    tc,
                    "ask" if permission_stage == HookStage.ON_PERMISSION_REQUEST else "deny",
                    reason,
                )
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
                    "A permission.response can record the decision; "
                    "the current tool call fails closed and is not replayed."
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

            # Execute
            if hasattr(tool, "ainvoke"):
                result = await tool.ainvoke(args)
            elif hasattr(tool, "invoke"):
                result = tool.invoke(args)
            elif callable(tool):
                result = tool(**args) if args else tool()
            else:
                result = f"Tool {tool_name} is not callable"

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
            "source": "permission_system",
            "tool_call": tool_call,
            "decision": decision,
            "reason": reason,
            "resume_supported": False,
        },
    }


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
    elif event_type == "client_message":
        data.setdefault("source", "send_message")
        data.setdefault("tool_call_id", tool_call_id)

    normalized["data"] = data
    return normalized


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
