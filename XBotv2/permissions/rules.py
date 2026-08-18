"""Permission-rule construction owned by the permissions plugin."""

from __future__ import annotations

import re
from typing import Any

from XBotv2.core.tools import ToolCall


def requested_permission_rule(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    tool = str(value.get("tool") or "").strip()
    params = value.get("params") or {}
    if not tool or not isinstance(params, dict):
        return {}
    for pattern in params.values():
        re.compile(str(pattern))
    rule: dict[str, Any] = {"tool": re.escape(tool)}
    if params:
        rule["params"] = {
            str(name): str(pattern)
            for name, pattern in params.items()
        }
    return rule

def permission_rule_for_tool_call(tool_call: ToolCall) -> dict[str, Any]:
    tool_name = tool_call.name
    if not tool_name:
        return {}
    rule: dict[str, Any] = {"tool": re.escape(tool_name)}
    args = tool_call.args
    if tool_name == "shell":
        args = {
            key: value
            for key, value in args.items()
            if key == "sandbox_permissions"
        }
    elif tool_name in {"edit", "path"}:
        retained = {
            "path", "source", "destination", "overwrite", "recursive", "parents"
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
