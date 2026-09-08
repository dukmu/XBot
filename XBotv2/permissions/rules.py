"""Permission-rule construction owned by the permissions plugin."""

from __future__ import annotations

import re
from typing import Any

from XBotv2.core.tools import ToolCall
from XBotv2.permissions.patterns import compile_pattern


def effective_args(tool: str, args: dict[str, Any], workspace: str | None) -> dict[str, Any]:
    """Match the shell's omitted/empty cwd using its runtime workspace default."""
    if tool == "shell" and not args.get("cwd") and workspace is not None:
        return {**args, "cwd": workspace}
    return args


def requested_permission_rule(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    tool = str(value.get("tool") or "").strip()
    params = value.get("params") or {}
    if not tool or not isinstance(params, dict):
        return {}
    for pattern in params.values():
        compile_pattern(str(pattern))
    rule: dict[str, Any] = {"tool": re.escape(tool)}
    if params:
        rule["params"] = {
            str(name): str(pattern)
            for name, pattern in params.items()
        }
    return rule

def permission_rule_for_tool_call(tool_call: ToolCall, *, workspace: str | None = None) -> dict[str, Any]:
    tool_name = tool_call.name
    if not tool_name:
        return {}
    rule: dict[str, Any] = {"tool": re.escape(tool_name)}
    args = effective_args(tool_name, tool_call.args, workspace)
    if tool_name == "shell":
        args = {
            key: value
            for key, value in args.items()
            if key in {"command", "cwd", "sandbox_permissions"}
        }
    elif tool_name in {"edit", "path"}:
        retained = {
            "path", "source", "destination", "overwrite", "recursive", "parents", "mode", "operation"
        }
        args = {key: value for key, value in args.items() if key in retained}
    params = {
        key: re.escape(str(value))
        for key, value in sorted(args.items())
        if isinstance(value, (str, int, float, bool))
    }
    if params:
        rule["params"] = params
    return rule
