"""Permission system for tool execution.

Deny → Allow → Ask → Default precedence.
Rules support regex matching on tool names, parameter values, and resolved
filesystem paths. Path expressions may contain runtime-variable references.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from xbotv2.api.variables import RuntimeVariables
from xbotv2.tools.filesystem_ops import PATH_ACCESS, TOOL_OPERATIONS

PermissionDecision = Literal["allow", "deny", "ask"]


@dataclass
class PermissionRule:
    """A single permission rule."""

    tool_pattern: str = ".*"  # Regex for tool name
    param_patterns: dict[str, str] = field(default_factory=dict)  # param → regex
    paths: str | None = None  # Resolved path regex or exact ${name} scope
    decision: PermissionDecision = "ask"


class PermissionSystem:
    """Tri-state permission system for tool execution.

    Checks tool calls against allow/deny/ask rules. Rules are checked
    in order: deny first (highest precedence), then allow, then ask.
    Falls back to the configured default.
    """

    def __init__(
        self,
        config: Any | None = None,
        *,
        default_decision: PermissionDecision = "ask",
        variables: RuntimeVariables | None = None,
    ) -> None:
        self.default_decision = default_decision
        self.variables = variables or RuntimeVariables()
        self._deny_rules: list[PermissionRule] = []
        self._allow_rules: list[PermissionRule] = []
        self._ask_rules: list[PermissionRule] = []

        if config is not None:
            self._load_config(config)

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load_config(self, config: Any) -> None:
        if hasattr(config, "model_dump"):
            data = config.model_dump()
        elif isinstance(config, dict):
            data = config
        else:
            return

        for rule_data in data.get("deny", []):
            self._deny_rules.append(self._parse_rule(rule_data, "deny"))
        for rule_data in data.get("allow", []):
            self._allow_rules.append(self._parse_rule(rule_data, "allow"))
        for rule_data in data.get("ask", []):
            self._ask_rules.append(self._parse_rule(rule_data, "ask"))

    def add_rule(self, decision: PermissionDecision, rule_data: dict[str, Any]) -> None:
        """Add one live permission rule to the in-memory policy."""
        rule = self._parse_rule(rule_data, decision)
        target = {
            "deny": self._deny_rules,
            "allow": self._allow_rules,
            "ask": self._ask_rules,
        }[decision]
        target.insert(0, rule)

    def replace_rules(self, config: Any | None) -> None:
        """Replace configured rules without invalidating shared references."""
        self._deny_rules.clear()
        self._allow_rules.clear()
        self._ask_rules.clear()
        if config is not None:
            self._load_config(config)

    def _parse_rule(
        self,
        data: dict,
        decision: PermissionDecision,
    ) -> PermissionRule:
        paths = data.get("paths")
        if paths is not None:
            if not isinstance(paths, str):
                raise ValueError("Permission paths must be a regular expression")
            try:
                re.compile(self.variables.expand_regex(
                    paths,
                    source="permission paths",
                ))
            except re.error as exc:
                raise ValueError(f"Invalid permission path regex: {exc}") from exc
        return PermissionRule(
            tool_pattern=data.get("tool", ".*"),
            param_patterns=data.get("params", {}),
            paths=paths,
            decision=decision,
        )

    # ------------------------------------------------------------------
    # Check
    # ------------------------------------------------------------------

    def check(self, tool_name: str, args: dict[str, Any] | None = None) -> PermissionDecision:
        """Check whether *tool_name* with *args* is allowed.

        Returns "allow", "deny", or "ask".
        """
        args = args or {}

        # Deny always wins
        for rule in self._deny_rules:
            if self._rule_matches(rule, tool_name, args):
                return "deny"

        # Allow checked second
        for rule in self._allow_rules:
            if self._rule_matches(rule, tool_name, args):
                return "allow"

        # Ask checked third
        for rule in self._ask_rules:
            if self._rule_matches(rule, tool_name, args):
                return "ask"

        return self.default_decision

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _rule_matches(
        self,
        rule: PermissionRule,
        tool_name: str,
        args: dict[str, Any],
    ) -> bool:
        if not re.fullmatch(rule.tool_pattern, tool_name):
            return False

        if rule.paths is not None and not self._all_paths_match(
            rule.paths, tool_name, args
        ):
            return False

        for param, pattern in rule.param_patterns.items():
            value = args.get(param)
            if value is None:
                return False
            if not re.fullmatch(pattern, str(value)):
                return False

        return True

    def _all_paths_match(
        self,
        pattern: str,
        tool_name: str,
        args: dict[str, Any],
    ) -> bool:
        operation = TOOL_OPERATIONS.get(tool_name)
        fields = PATH_ACCESS.get(operation or "", ())
        if not fields:
            return False
        reference = self.variables.reference_name(
            pattern,
            source="permission paths",
        )
        root = Path(self.variables[reference]) if reference is not None else None
        expanded = (
            None
            if root is not None
            else self.variables.expand_regex(
                pattern,
                source="permission paths",
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
            elif expanded is not None and not re.fullmatch(expanded, str(resolved)):
                return False
        return True


class PermissionIntersection:
    """Return the more restrictive decision from parent and child policy."""

    def __init__(self, parent: Any, child: Any) -> None:
        self.parent = parent
        self.child = child

    def check(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
    ) -> PermissionDecision:
        decisions = {
            self.parent.check(tool_name, args),
            self.child.check(tool_name, args),
        }
        if "deny" in decisions:
            return "deny"
        if "ask" in decisions:
            return "ask"
        return "allow"
