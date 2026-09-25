"""Permission-rule construction owned by the permissions plugin."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pydantic import JsonValue

from XBotv2.core.tools import ToolCall
from XBotv2.permissions.contracts import PermissionDecision, PermissionRule
from XBotv2.permissions.patterns import compile_pattern


def effective_args(
    tool: str,
    args: dict[str, JsonValue],
    workspace: str | None,
) -> dict[str, JsonValue]:
    """Match the shell's omitted/empty cwd using its runtime workspace default."""
    if tool == "shell" and not args.get("cwd") and workspace is not None:
        return {**args, "cwd": workspace}
    return args


def requested_permission_rule(
    value: Mapping[str, JsonValue],
    *,
    decision: PermissionDecision,
) -> PermissionRule:
    tool = str(value.get("tool") or "").strip()
    params = value.get("params") or {}
    if not tool or not isinstance(params, dict):
        raise ValueError("Named permission requires a tool and parameter mapping")
    for pattern in params.values():
        compile_pattern(str(pattern))
    return PermissionRule(
        tool_pattern=re.escape(tool),
        param_patterns={str(name): str(pattern) for name, pattern in params.items()},
        path_scope=None,
        decision=decision,
    )

def permission_rule_for_tool_call(
    tool_call: ToolCall,
    *,
    workspace: str | None = None,
    selectors: tuple[str, ...] | None = None,
    decision: PermissionDecision,
) -> PermissionRule:
    """Mint the rule covering one tool call's authorization scope.

    ``selectors`` are the tool owner's declared scope arguments
    (``Tool.grant_selectors``); when the owner declares none, every scalar
    argument is constrained. The permissions package therefore never keeps
    its own copy of another package's argument vocabulary — a new argument
    is covered (narrowed) automatically instead of silently widening a
    previously minted grant.
    """
    tool_name = tool_call.name
    if not tool_name:
        raise ValueError("Tool permission requires a named tool call")
    args = effective_args(tool_name, tool_call.args, workspace)
    if selectors:
        args = {key: value for key, value in args.items() if key in selectors}
    params = {
        key: re.escape(str(value))
        for key, value in sorted(args.items())
        if isinstance(value, (str, int, float, bool))
    }
    return PermissionRule(
        tool_pattern=re.escape(tool_name),
        param_patterns=params,
        path_scope=None,
        decision=decision,
    )
