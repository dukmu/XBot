"""Permission system for tool execution.

Deny → Allow → Ask → Default precedence.
Rules support regex matching on tool names and parameter values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

PermissionDecision = Literal["allow", "deny", "ask"]


@dataclass
class PermissionRule:
    """A single permission rule."""

    tool_pattern: str = ".*"  # Regex for tool name
    param_patterns: dict[str, str] = field(default_factory=dict)  # param → regex
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
    ) -> None:
        self.default_decision = default_decision
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

    @staticmethod
    def _parse_rule(data: dict, decision: PermissionDecision) -> PermissionRule:
        return PermissionRule(
            tool_pattern=data.get("tool", ".*"),
            param_patterns=data.get("params", {}),
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

    @staticmethod
    def _rule_matches(rule: PermissionRule, tool_name: str, args: dict[str, Any]) -> bool:
        if not re.fullmatch(rule.tool_pattern, tool_name):
            return False

        for param, pattern in rule.param_patterns.items():
            value = args.get(param)
            if value is None:
                return False
            if not re.fullmatch(pattern, str(value)):
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
