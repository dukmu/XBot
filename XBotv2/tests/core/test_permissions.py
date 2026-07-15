"""Tests for PermissionSystem."""

from pathlib import Path

import pytest
import yaml

from xbotv2.tools.permissions import PermissionSystem, PermissionRule


class TestPermissionRule:
    """Rule matching."""

    def test_default_rule_matches_all(self):
        """Default rule matches any tool."""
        rule = PermissionRule()
        assert PermissionSystem._rule_matches(rule, "any_tool", {})

    def test_tool_pattern_match(self):
        """Rule matches by tool name regex."""
        rule = PermissionRule(tool_pattern="shell_.*")
        assert PermissionSystem._rule_matches(rule, "shell_exec", {})
        assert not PermissionSystem._rule_matches(rule, "filesystem_read", {})

    def test_param_pattern_match(self):
        """Rule matches by parameter value regex."""
        rule = PermissionRule(
            tool_pattern="filesystem_.*",
            param_patterns={"path": "/tmp/.*"},
        )
        assert PermissionSystem._rule_matches(
            rule, "filesystem_read", {"path": "/tmp/test.txt"}
        )
        assert not PermissionSystem._rule_matches(
            rule, "filesystem_read", {"path": "/etc/passwd"}
        )

    def test_param_missing_does_not_match(self):
        """Missing parameter causes no match."""
        rule = PermissionRule(param_patterns={"path": ".*"})
        assert not PermissionSystem._rule_matches(rule, "tool", {})


class TestPermissionSystemBasics:
    """Basic permission checks."""

    def test_default_decision(self, permission_system):
        """When no rules match, default is used."""
        decision = permission_system.check("any_tool", {})
        assert decision == permission_system.default_decision

    def test_deny_wins(self, permission_system):
        """Deny always takes precedence."""
        permission_system._deny_rules.append(
            PermissionRule(tool_pattern=".*", decision="deny")
        )
        permission_system._allow_rules.append(
            PermissionRule(tool_pattern=".*", decision="allow")
        )
        decision = permission_system.check("any_tool", {})
        assert decision == "deny"

    def test_allow_before_ask(self, permission_system):
        """Allow is checked before ask."""
        permission_system._allow_rules.append(
            PermissionRule(tool_pattern="shell", decision="allow")
        )
        permission_system._ask_rules.append(
            PermissionRule(tool_pattern="shell", decision="ask")
        )
        decision = permission_system.check("shell", {})
        assert decision == "allow"

    def test_ask_falls_back_to_default(self, permission_system):
        """If no allow/deny matches, ask rules are checked."""
        permission_system.default_decision = "allow"
        permission_system._ask_rules.append(
            PermissionRule(tool_pattern="shell", decision="ask")
        )
        decision = permission_system.check("other_tool", {})
        assert decision == "allow"  # Default, not ask

    def test_tool_specific_rule(self, permission_system):
        """Rules can target specific tools."""
        permission_system._allow_rules.append(
            PermissionRule(tool_pattern="filesystem_read", decision="allow")
        )
        assert permission_system.check("filesystem_read", {}) == "allow"
        assert permission_system.check("filesystem_write", {}) == permission_system.default_decision


class TestConfigLoading:
    """Loading rules from config."""

    def test_load_from_dict(self):
        """Rules are loaded from a config dict."""
        ps = PermissionSystem(
            config={
                "deny": [{"tool": "dangerous_.*"}],
                "allow": [{"tool": "safe_.*"}],
                "ask": [{"tool": "maybe_.*"}],
            }
        )
        assert ps.check("dangerous_tool", {}) == "deny"
        assert ps.check("safe_tool", {}) == "allow"
        assert ps.check("maybe_tool", {}) == "ask"

    def test_load_with_params(self):
        """Rules can include param patterns."""
        ps = PermissionSystem(
            config={
                "allow": [
                    {
                        "tool": "filesystem_.*",
                        "params": {"path": "/tmp/.*"},
                    },
                ],
            }
        )
        assert ps.check("filesystem_read", {"path": "/tmp/ok.txt"}) == "allow"
        assert ps.check("filesystem_read", {"path": "/etc/bad"}) == ps.default_decision

    def test_empty_config(self):
        """Empty config yields no rules."""
        ps = PermissionSystem(config={})
        assert len(ps._deny_rules) == 0
        assert len(ps._allow_rules) == 0
        assert len(ps._ask_rules) == 0

    def test_shipped_policy_allows_internal_and_interaction_tools(self):
        config = yaml.safe_load(
            Path("XBotv2/data/config/permissions.yaml").read_text(
                encoding="utf-8"
            )
        )
        permissions = PermissionSystem(config=config)

        for tool_name in (
            "send_message",
            "ask_user",
            "search_text",
            "find_files",
            "list_tasks",
            "stop_task",
            "list_todos",
            "create_todo",
            "update_todo",
            "remove_todo",
        ):
            assert permissions.check(tool_name, {}) == "allow"

        assert permissions.check("shell", {"command": "echo risky"}) == "ask"
        assert permissions.check("shell", {"command": "date"}) == "ask"
        assert permissions.check("unknown_tool", {}) == "ask"
