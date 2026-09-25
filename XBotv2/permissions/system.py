"""Canonical permission-policy evaluation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import JsonValue

from XBotv2.core.filesystem.operations import PATH_ACCESS, resolve_operation
from XBotv2.core.tools import ToolCall
from XBotv2.core.variables import RuntimeVariables
from XBotv2.permissions.contracts import (
    PermissionDecision,
    PermissionPolicy,
    PermissionRule,
    PermissionsPort,
)
from XBotv2.permissions.patterns import compile_pattern, fullmatch, matching_budget
from XBotv2.permissions.rules import effective_args


def _permission_value(value: JsonValue) -> str:
    if isinstance(value, (Mapping, list, tuple)):
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Permission parameters must be JSON-compatible") from exc
    return str(value)


def _validate_rule(rule: PermissionRule, variables: RuntimeVariables) -> None:
    compile_pattern(rule.tool_pattern)
    for pattern in rule.param_patterns.values():
        compile_pattern(pattern)
    if rule.path_scope is not None:
        compile_pattern(
            variables.expand_regex(rule.path_scope, source="permission path scope")
        )


def _grant_rule(tool_name: str, param_patterns: Mapping[str, str]) -> PermissionRule:
    if not tool_name.strip():
        raise ValueError("Permission tool name must not be empty")
    return PermissionRule(
        tool_pattern=re.escape(tool_name),
        param_patterns={
            str(name): str(pattern) for name, pattern in param_patterns.items()
        },
        path_scope=None,
        decision="allow",
    )


class PermissionSystem:
    """Evaluate canonical policies with deny/grant/allow/ask precedence."""

    def __init__(
        self,
        policy: PermissionPolicy | None = None,
        *,
        policies: Sequence[PermissionPolicy] = (),
        variables: RuntimeVariables | None = None,
        parent: PermissionsPort | None = None,
    ) -> None:
        self.variables = variables or RuntimeVariables()
        self.parent = parent
        self._policies: tuple[PermissionPolicy, ...] = ()
        self._session_grants: tuple[PermissionRule, ...] = ()
        self._once_grants: list[PermissionRule] = []
        initial = (policy,) if policy is not None else ()
        self.replace_policies((*initial, *policies))

    def replace_policies(self, policies: Sequence[PermissionPolicy]) -> None:
        resolved = tuple(policies)
        for policy in resolved:
            for rule in policy.rules:
                _validate_rule(rule, self.variables)
        self._policies = resolved

    def replace_session_grants(self, grants: Sequence[PermissionRule]) -> None:
        resolved = tuple(grants)
        for rule in resolved:
            if rule.decision != "allow":
                raise ValueError("Session permission grants must allow")
            _validate_rule(rule, self.variables)
        self._session_grants = resolved

    def grant_once(self, tool_name: str, param_patterns: dict[str, str]) -> None:
        rule = _grant_rule(tool_name, param_patterns)
        _validate_rule(rule, self.variables)
        self._once_grants.append(rule)

    def check(
        self,
        tool_name: str,
        args: dict[str, JsonValue] | None = None,
    ) -> PermissionDecision:
        with matching_budget():
            effective = effective_args(
                tool_name,
                args or {},
                self.variables.get("workspace"),
            )
            local = self._check_local(tool_name, effective)
            if self.parent is None:
                return local
            return _meet(self.parent.check(tool_name, effective), local)

    def _check_local(
        self,
        tool_name: str,
        args: dict[str, JsonValue],
    ) -> PermissionDecision:
        if any(
            rule.decision == "deny" and self._rule_matches(rule, tool_name, args)
            for policy in self._policies
            for rule in policy.rules
        ):
            return "deny"
        if any(
            self._rule_matches(rule, tool_name, args)
            for rule in (*self._once_grants, *self._session_grants)
        ):
            return "allow"
        decisions = tuple(
            self._policy_decision(policy, tool_name, args)
            for policy in self._policies
        )
        if "deny" in decisions:
            return "deny"
        if "ask" in decisions:
            return "ask"
        return "allow"

    def _policy_decision(
        self,
        policy: PermissionPolicy,
        tool_name: str,
        args: dict[str, JsonValue],
    ) -> PermissionDecision:
        for decision in ("allow", "ask"):
            if any(
                rule.decision == decision
                and self._rule_matches(rule, tool_name, args)
                for rule in policy.rules
            ):
                return decision
        return policy.default_decision

    def consume_once(self, tool_name: str, args: dict[str, JsonValue]) -> None:
        with matching_budget():
            effective = effective_args(
                tool_name,
                args,
                self.variables.get("workspace"),
            )
            for index, grant in enumerate(self._once_grants):
                if self._rule_matches(grant, tool_name, effective):
                    self._once_grants.pop(index)
                    return
            if self.parent is not None:
                self.parent.consume_once(tool_name, effective)

    def explicit_allow(
        self,
        tool_name: str,
        args: dict[str, JsonValue] | None = None,
        *,
        constrain_param: str | None = None,
    ) -> bool:
        with matching_budget():
            effective = effective_args(
                tool_name,
                args or {},
                self.variables.get("workspace"),
            )
            if self.parent is not None and not self.parent.explicit_allow(
                tool_name,
                effective,
                constrain_param=constrain_param,
            ):
                return False
            if any(
                rule.decision == "deny"
                and self._rule_matches(rule, tool_name, effective)
                for policy in self._policies
                for rule in policy.rules
            ):
                return False
            candidates = (
                *self._once_grants,
                *self._session_grants,
                *(
                    rule
                    for policy in self._policies
                    for rule in policy.rules
                    if rule.decision == "allow"
                ),
            )
            return any(
                (constrain_param is None or constrain_param in rule.param_patterns)
                and self._rule_matches(rule, tool_name, effective)
                for rule in candidates
            )

    def check_tool_call(self, tool_call: ToolCall) -> tuple[PermissionDecision, str]:
        with matching_budget():
            return self._check_tool_call(tool_call)

    def _check_tool_call(self, tool_call: ToolCall) -> tuple[PermissionDecision, str]:
        tool_name = tool_call.name
        args = dict(tool_call.args)
        escalated = (
            tool_name == "shell"
            and args.get("sandbox_permissions") == "require_escalated"
        )
        escape_allowed = not escalated or self.explicit_allow(
            tool_name,
            args,
            constrain_param="sandbox_permissions",
        )
        decision = self.check(tool_name, args)
        if escalated and decision == "allow" and not escape_allowed:
            decision = "ask"
        if decision == "deny":
            return decision, f"Permission denied for tool: {tool_name}"
        if decision == "ask" and escalated:
            justification = str(args.get("justification") or "").strip()
            return decision, (
                f"Sandbox escape requires human approval: {justification}"
                if justification
                else "Sandbox escape requires human approval."
            )
        if decision == "ask":
            return decision, f"Permission approval required for tool: {tool_name}."
        self.consume_once(tool_name, args)
        return decision, ""

    def _rule_matches(
        self,
        rule: PermissionRule,
        tool_name: str,
        args: dict[str, JsonValue],
    ) -> bool:
        if not fullmatch(rule.tool_pattern, tool_name):
            return False
        if not all(
            name in args and fullmatch(pattern, _permission_value(args[name]))
            for name, pattern in rule.param_patterns.items()
        ):
            return False
        return rule.path_scope is None or self._all_paths_match(
            rule.path_scope,
            tool_name,
            args,
        )

    def _all_paths_match(
        self,
        pattern: str,
        tool_name: str,
        args: dict[str, JsonValue],
    ) -> bool:
        operation = resolve_operation(tool_name, args)
        fields = PATH_ACCESS.get(operation or "", ())
        if not fields:
            return False
        reference = self.variables.reference_name(
            pattern,
            source="permission path scope",
        )
        root = Path(self.variables[reference]) if reference is not None else None
        expanded = (
            None
            if root is not None
            else self.variables.expand_regex(
                pattern,
                source="permission path scope",
            )
        )
        workspace = self.variables.get("workspace")
        for field, _access in fields:
            value = args.get(field)
            if not isinstance(value, str):
                return False
            path = Path(value).expanduser()
            try:
                if path.is_absolute():
                    resolved = path.resolve()
                elif workspace is not None:
                    resolved = (Path(workspace) / path).resolve()
                else:
                    return False
            except (OSError, RuntimeError):
                return False
            if root is not None:
                if not resolved.is_relative_to(root):
                    return False
            elif expanded is not None and not fullmatch(expanded, str(resolved)):
                return False
        return True


def _meet(left: PermissionDecision, right: PermissionDecision) -> PermissionDecision:
    if "deny" in (left, right):
        return "deny"
    if "ask" in (left, right):
        return "ask"
    return "allow"


__all__ = ["PermissionSystem"]
