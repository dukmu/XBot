"""Tests for PermissionSystem."""

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from xbotv2.tools.permissions import (
    PermissionIntersection,
    PermissionRule,
    PermissionSystem,
)
from xbotv2.api import ToolCall
from xbotv2.config.policy import _permission_rule_for_tool_call
from xbotv2.api.paths import RuntimePaths
from xbotv2.api.variables import RuntimeVariables
from xbotv2.core.operations import update_session_policy
from xbotv2.tools.sandbox import SandboxPolicy


class TestPermissionRule:
    """Rule matching."""

    def test_default_rule_matches_all(self):
        """Default rule matches any tool."""
        rule = PermissionRule()
        assert PermissionSystem()._rule_matches(rule, "any_tool", {})

    def test_tool_pattern_match(self):
        """Rule matches by tool name regex."""
        rule = PermissionRule(tool_pattern="shell_.*")
        permissions = PermissionSystem()
        assert permissions._rule_matches(rule, "shell_exec", {})
        assert not permissions._rule_matches(rule, "filesystem_read", {})

    def test_param_pattern_match(self):
        """Rule matches by parameter value regex."""
        rule = PermissionRule(
            tool_pattern="filesystem_.*",
            param_patterns={"path": "/tmp/.*"},
        )
        permissions = PermissionSystem()
        assert permissions._rule_matches(
            rule, "filesystem_read", {"path": "/tmp/test.txt"}
        )
        assert not permissions._rule_matches(
            rule, "filesystem_read", {"path": "/etc/passwd"}
        )

    def test_param_missing_does_not_match(self):
        """Missing parameter causes no match."""
        rule = PermissionRule(param_patterns={"path": ".*"})
        assert not PermissionSystem()._rule_matches(rule, "tool", {})


class TestPermissionSystemBasics:
    """Basic permission checks."""

    def test_default_decision(self, permission_system):
        """When no rules match, default is used."""
        decision = permission_system.check("any_tool", {})
        assert decision == permission_system.default_decision

    def test_replacing_rules_preserves_parent_intersection_reference(self):
        parent = PermissionSystem({"deny": [{"tool": "shell"}]})
        child = PermissionSystem({"ask": [{"tool": "shell"}]})
        permissions = PermissionIntersection(parent, child)

        parent.replace_rules({"allow": [{"tool": "shell"}]})
        child.replace_rules({"allow": [{"tool": "shell"}]})

        assert permissions.parent is parent
        assert permissions.child is child
        assert permissions.check("shell") == "allow"

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

    def test_filesystem_write_session_rule_records_only_path(self):
        rule = _permission_rule_for_tool_call(ToolCall(
            "call-1",
            "filesystem_write",
            {"path": "notes.md", "content": "large private document"},
        ))

        assert rule == {
            "tool": "filesystem_write",
            "params": {"path": "notes\\.md"},
        }
        permissions = PermissionSystem({"allow": [rule]})
        assert permissions.check(
            "filesystem_write",
            {"path": "notes.md", "content": "different content"},
        ) == "allow"
        assert permissions.check(
            "filesystem_write",
            {"path": "other.md", "content": "large private document"},
        ) == "ask"

    def test_filesystem_patch_rule_does_not_record_patch_body(self):
        rule = _permission_rule_for_tool_call(ToolCall(
            "call-1",
            "filesystem_patch",
            {"path": "code.py", "patch": "private patch body"},
        ))

        assert rule == {
            "tool": "filesystem_patch",
            "params": {"path": "code\\.py"},
        }

    def test_filesystem_move_rule_records_both_paths_and_overwrite(self):
        rule = _permission_rule_for_tool_call(ToolCall(
            "call-1",
            "filesystem_move",
            {"source": "a.txt", "destination": "b.txt", "overwrite": True},
        ))

        assert rule == {
            "tool": "filesystem_move",
            "params": {
                "destination": "b\\.txt",
                "overwrite": "True",
                "source": "a\\.txt",
            },
        }


@pytest.mark.asyncio
async def test_session_policy_reload_cannot_expand_child_past_parent(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    paths.session("s").root.mkdir(parents=True)
    parent_permissions = PermissionSystem({"deny": [{"tool": "shell"}]})
    child_permissions = PermissionSystem({"allow": [{"tool": "shell"}]})
    intersection = PermissionIntersection(parent_permissions, child_permissions)

    def runtime(thread_id, permissions, base_permissions):
        sandbox = SandboxPolicy(
            {},
            data_root=paths.data_dir,
            workspace_root=tmp_path,
            session_root=paths.session("s").thread(thread_id).root,
        )
        engine = SimpleNamespace(
            permission_system=permissions,
            sandbox_policy=sandbox,
            startup_config=SimpleNamespace(
                permissions=base_permissions,
                sandbox={},
            ),
            config=SimpleNamespace(permissions={}, sandbox={}),
        )
        return SimpleNamespace(
            session_id="s",
            thread_id=thread_id,
            paths=paths,
            workspace_root=str(tmp_path),
            engine=engine,
            turn_lock=asyncio.Lock(),
        )

    parent = runtime(
        "agent", parent_permissions, {"deny": [{"tool": "shell"}]}
    )
    child = runtime(
        "child", intersection, {"allow": [{"tool": "shell"}]}
    )

    await update_session_policy(
        paths=paths,
        session_id="s",
        contexts=[parent, child],
        permissions={"shell": "allow"},
    )

    assert parent.engine.permission_system is parent_permissions
    assert child.engine.permission_system is intersection
    assert intersection.parent is parent_permissions
    assert intersection.check("shell") == "deny"


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

    def test_workspace_scope_resolves_all_filesystem_paths(self, tmp_path):
        workspace = tmp_path / "workspace"
        outside = tmp_path / "outside"
        workspace.mkdir()
        outside.mkdir()
        (workspace / "link").symlink_to(outside, target_is_directory=True)
        (workspace / "loop").symlink_to("loop")
        permissions = PermissionSystem(
            {
                "allow": [{
                    "tool": "filesystem_.*",
                    "paths": "${workspace}",
                }],
            },
            variables=RuntimeVariables({"workspace": workspace}),
        )

        assert permissions.check(
            "filesystem_write", {"path": "notes.md"}
        ) == "allow"
        assert permissions.check(
            "filesystem_write", {"path": str(workspace / "notes.md")}
        ) == "allow"
        assert permissions.check(
            "filesystem_write", {"path": "../outside/notes.md"}
        ) == "ask"
        assert permissions.check(
            "filesystem_write", {"path": "link/notes.md"}
        ) == "ask"
        assert permissions.check(
            "filesystem_write", {"path": "loop/notes.md"}
        ) == "ask"
        assert permissions.check("filesystem_move", {
            "source": "old.md",
            "destination": "archive/new.md",
        }) == "allow"
        assert permissions.check("filesystem_move", {
            "source": "old.md",
            "destination": str(outside / "new.md"),
        }) == "ask"

    def test_path_scope_supports_regex_and_workspace_expansion(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        permissions = PermissionSystem(
            {"allow": [{
                "tool": "filesystem_write",
                "paths": "${workspace}/generated/.*",
            }]},
            variables=RuntimeVariables({"workspace": workspace}),
        )

        assert permissions.check(
            "filesystem_write", {"path": "generated/result.txt"}
        ) == "allow"
        assert permissions.check(
            "filesystem_write", {"path": "src/result.txt"}
        ) == "ask"

        absolute = PermissionSystem({
            "allow": [{
                "tool": "filesystem_read",
                "paths": re.escape(str(tmp_path)) + "/allowed/.*",
            }],
        })
        assert absolute.check(
            "filesystem_read", {"path": str(tmp_path / "allowed/file.txt")}
        ) == "allow"

    def test_unknown_path_variable_is_rejected(self):
        with pytest.raises(ValueError, match="Unknown runtime variable"):
            PermissionSystem({
                "allow": [{"tool": "filesystem_write", "paths": "${unknown}"}],
            })

    def test_invalid_path_regex_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid permission path regex"):
            PermissionSystem({
                "allow": [{"tool": "filesystem_write", "paths": "["}],
            })

    def test_empty_config(self):
        """Empty config yields no rules."""
        ps = PermissionSystem(config={})
        assert len(ps._deny_rules) == 0
        assert len(ps._allow_rules) == 0
        assert len(ps._ask_rules) == 0

    def test_shipped_policy_allows_internal_and_workspace_tools(self, tmp_path):
        config = yaml.safe_load(
            Path("XBotv2/data/config/permissions.yaml").read_text(
                encoding="utf-8"
            )
        )
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        permissions = PermissionSystem(
            config=config,
            variables=RuntimeVariables({"workspace": workspace}),
        )

        for tool_name in (
            "send_message",
            "ask_user",
            "search_text",
            "find_files",
            "list_tasks",
            "stop_task",
            "update_todos",
        ):
            assert permissions.check(tool_name, {}) == "allow"

        assert permissions.check("filesystem_read", {"path": "README.md"}) == "allow"
        assert permissions.check("filesystem_stat", {"path": "README.md"}) == "allow"
        assert permissions.check("filesystem_list", {"path": "."}) == "allow"
        for tool_name in (
            "filesystem_write",
            "filesystem_edit",
            "filesystem_patch",
            "filesystem_delete",
            "filesystem_mkdir",
        ):
            assert permissions.check(tool_name, {"path": "notes.md"}) == "allow"
            assert permissions.check(
                tool_name, {"path": str(tmp_path / "outside.md")}
            ) == "ask"
        for tool_name in ("filesystem_move", "filesystem_copy"):
            assert permissions.check(tool_name, {
                "source": "source.md",
                "destination": "destination.md",
            }) == "allow"
            assert permissions.check(tool_name, {
                "source": "source.md",
                "destination": str(tmp_path / "outside.md"),
            }) == "ask"
        assert permissions.check("shell", {"command": "echo allowed"}) == "allow"
        assert permissions.check("unknown_tool", {}) == "ask"
