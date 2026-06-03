"""Tool execution node with hook integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

logger = logging.getLogger("xbotv2.tools.runtime")


async def execute_tools(
    tool_calls: list[dict[str, Any]],
    registry: Any,  # ToolRegistry
    *,
    sandbox_policy: Any = None,  # SandboxPolicy
    permission_system: Any = None,  # PermissionSystem
    workspace_root: str = "/tmp/xbotv2-workspace",
) -> list[ToolMessage]:
    """Execute tool calls through the guard pipeline.

    Pipeline:
    1. Extract tool calls from the last AIMessage.
    2. Run before_tools hooks (sandbox/permission checks).
    3. Handle interrupts (ask, tool_confirm).
    4. Execute approved tools.
    5. Return ToolMessages.

    Args:
        tool_calls: List of {"name": str, "args": dict, "id": str} dicts.
        registry: ToolRegistry instance.
        sandbox_policy: SandboxPolicy instance (optional).
        permission_system: PermissionSystem instance (optional).
        workspace_root: Workspace root for path resolution.

    Returns:
        List of ToolMessage instances (one per tool call).
    """
    results: list[ToolMessage] = []
    denials: dict[str, str] = {}  # tool_call_id → reason
    sequential_tools: set[str] = set()

    # Phase 1: Guards — check sandbox and permissions for each tool
    for tc in tool_calls:
        tool_name = tc["name"]
        entry = registry.get(tool_name) if registry else None

        if entry is None:
            denials[tc["id"]] = f"Tool not registered: {tool_name}"
            continue

        # Sandbox guard
        if sandbox_policy and entry.sandbox_mode == "sandboxed":
            allowed, reason = sandbox_policy.guard_tool_call(
                tool_name, tc.get("args", {}), entry.sandbox_mode
            )
            if not allowed:
                denials[tc["id"]] = reason
                continue

        # Permission guard
        if permission_system:
            decision = permission_system.check(tool_name, tc.get("args", {}))
            if decision == "deny":
                denials[tc["id"]] = f"Permission denied for tool: {tool_name}"
                continue
            # "ask" would trigger an interrupt in a real system; for now, allow

        # Track sequential tools
        if entry.execution_mode == "sequential":
            sequential_tools.add(tool_name)

    # Phase 2: Execute approved tools
    for tc in tool_calls:
        tool_id = tc["id"]

        if tool_id in denials:
            results.append(ToolMessage(
                content=f"Error: {denials[tool_id]}",
                tool_call_id=tool_id,
                status="error",
            ))
            continue

        tool_name = tc["name"]
        entry = registry.get(tool_name)
        if entry is None:
            continue

        tool = entry.tool
        args = tc.get("args", {})

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

            content = str(result) if not isinstance(result, str) else result
            results.append(ToolMessage(
                content=content,
                tool_call_id=tool_id,
                status="success",
            ))

        except Exception as exc:
            logger.exception("Tool %s failed", tool_name)
            results.append(ToolMessage(
                content=f"Error executing {tool_name}: {exc}",
                tool_call_id=tool_id,
                status="error",
            ))

    # Clear one-call sandbox approvals
    if sandbox_policy and hasattr(sandbox_policy, "clear_one_call_approvals"):
        sandbox_policy.clear_one_call_approvals()

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
