"""Tests for PermissionSystem."""

import asyncio
import re
from typing import Any
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from XBotv2.permissions.system import (
    PermissionIntersection,
    PermissionSystem,
)
from XBotv2.core import ToolCall
from XBotv2.config.policy import (
    _permission_rule_for_tool_call,
    persist_permission_decision,
)
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.variables import RuntimeVariables
from XBotv2.coretools.filesystem import FILESYSTEM_TOOLS
from XBotv2.session.operations import update_session_policy
from XBotv2.sandbox.filesystem_ops import PATH_ACCESS, resolve_operation
from XBotv2.sandbox.policy import SandboxPolicy


class TestPermissionSystemBasics:
    """Basic permission checks."""

    def test_default_decision(self, permission_system):
        """When no rules match, default is used."""
        decision = permission_system.check("any_tool", {})
        assert decision == permission_system.default_decision

    def test_replacing_rules_updates_intersection_behavior(self):
        parent = PermissionSystem({"deny": [{"tool": "shell"}]})
        child = PermissionSystem({"ask": [{"tool": "shell"}]})
        permissions = PermissionIntersection(parent, child)

        assert permissions.check("shell") == "deny"
        parent.replace_rules({"allow": [{"tool": "shell"}]})
        child.replace_rules({"allow": [{"tool": "shell"}]})

        assert permissions.check("shell") == "allow"

    def test_deny_wins(self):
        permissions = PermissionSystem({
            "deny": [{"tool": ".*"}],
            "allow": [{"tool": ".*"}],
        })
        assert permissions.check("any_tool") == "deny"

    def test_allow_before_ask(self):
        permissions = PermissionSystem({
            "allow": [{"tool": "shell"}],
            "ask": [{"tool": "shell"}],
        })
        assert permissions.check("shell") == "allow"

    def test_ask_falls_back_to_default(self):
        permissions = PermissionSystem(
            {"ask": [{"tool": "shell"}]},
            default_decision="allow",
        )
        assert permissions.check("other_tool") == "allow"

    def test_allow_once_is_exact_and_consumed(self):
        permissions = PermissionSystem(default_decision="ask")
        arguments = {"path": "report.txt", "mode": "write", "content": "done"}

        permissions.grant_once(
            "edit",
            {"path": r"report\.txt", "content": "done"},
        )

        assert permissions.check(
            "edit",
            {**arguments, "content": "other"},
        ) == "ask"
        assert permissions.check("edit", arguments) == "allow"
        assert permissions.check("edit", arguments) == "ask"

        denied = PermissionSystem({"deny": [{"tool": "edit"}]})
        denied.grant_once(
            "edit",
            {"path": r"report\.txt", "content": "done"},
        )
        assert denied.check("edit", arguments) == "deny"
        denied.replace_rules(None)
        assert denied.check("edit", arguments) == "ask"

    def test_filesystem_write_session_rule_records_only_path(self):
        rule = _permission_rule_for_tool_call(ToolCall(
            "call-1",
            "edit",
            {"path": "notes.md", "mode": "write", "content": "large private document"},
        ))

        assert rule == {
            "tool": "edit",
            "params": {"path": "notes\\.md"},
        }
        permissions = PermissionSystem({"allow": [rule]})
        assert permissions.check(
            "edit",
            {"path": "notes.md", "mode": "write", "content": "different content"},
        ) == "allow"
        assert permissions.check(
            "edit",
            {"path": "other.md", "mode": "write", "content": "large private document"},
        ) == "ask"

    def test_filesystem_move_rule_records_both_paths_and_overwrite(self):
        rule = _permission_rule_for_tool_call(ToolCall(
            "call-1",
            "path",
            {"operation": "move", "source": "a.txt", "destination": "b.txt", "overwrite": True},
        ))

        assert rule == {
            "tool": "path",
            "params": {
                "destination": "b\\.txt",
                "overwrite": "True",
                "source": "a\\.txt",
            },
        }

    def test_requested_session_rule_is_persisted(self, tmp_path):
        paths = RuntimePaths.from_data_dir(tmp_path / "data")
        persist_permission_decision(
            paths=paths,
            session_id="permission-rule",
            client_event={
                "data": {
                    "source": "request_permission",
                    "permission": {
                        "tool": "mcp__github__search",
                        "params": {"query": r"issues/.*"},
                    },
                },
            },
            decision="allow",
            scope="session",
        )

        policy = yaml.safe_load(
            paths.session("permission-rule").config_file.read_text(
                encoding="utf-8"
            )
        )
        assert policy["permissions"]["allow"] == [{
            "tool": "mcp__github__search",
            "params": {"query": r"issues/.*"},
        }]

    def test_sandbox_session_approval_persists_and_applies_path(self, tmp_path):
        paths = RuntimePaths.from_data_dir(tmp_path / "data")
        workspace = tmp_path / "workspace"
        outside = tmp_path / "outside"
        workspace.mkdir()
        outside.mkdir()
        sandbox = SandboxPolicy(
            {},
            data_root=paths.data_dir,
            workspace_root=workspace,
        )
        engine = SimpleNamespace(
            workspace_root=workspace,
            sandbox_policy=sandbox,
        )

        persist_permission_decision(
            paths=paths,
            session_id="sandbox-rule",
            client_event={"data": {
                "source": "sandbox",
                "tool_call": {
                    "id": "call-1",
                    "name": "edit",
                    "args": {"path": str(outside / "report.txt")},
                },
            }},
            decision="allow",
            scope="session",
            engine=engine,
            sandbox_rules=[{
                "path": str(outside),
                "access": "readwrite",
            }],
        )

        policy = yaml.safe_load(
            paths.session("sandbox-rule").config_file.read_text(encoding="utf-8")
        )
        assert policy["sandbox"] == {
            "enabled": True,
            "resources": [{"path": str(outside), "access": "readwrite"}],
        }
        assert sandbox.check_tool_access(
            "edit", {"path": str(outside / "report.txt")}
        ) == []

    def test_shell_escalation_approval_updates_external_write_policy(
        self,
        tmp_path,
    ):
        paths = RuntimePaths.from_data_dir(tmp_path / "data")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sandbox = SandboxPolicy(
            {"external_write": "ask"},
            data_root=paths.data_dir,
            workspace_root=workspace,
        )
        engine = SimpleNamespace(sandbox_policy=sandbox)

        persist_permission_decision(
            paths=paths,
            session_id="shell-escalation",
            client_event={"data": {"source": "sandbox"}},
            decision="allow",
            scope="session",
            engine=engine,
            sandbox_rules=[{
                "setting": "external_write",
                "access": "readwrite",
            }],
        )

        policy = yaml.safe_load(
            paths.session("shell-escalation").config_file.read_text(
                encoding="utf-8"
            )
        )
        assert policy["sandbox"] == {
            "enabled": True,
            "external_write": "allow",
        }
        assert sandbox.external_write == "allow"


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
            job_registry=None,
            permission_system=permissions,
            sandbox_policy=sandbox,
            config=SimpleNamespace(permissions={}, sandbox={}),
            state_store=SimpleNamespace(
                read_thread_metadata=lambda: {
                    "agent_definition": {
                        "name": thread_id,
                        "description": "test agent",
                        "permissions": base_permissions,
                    }
                }
            ),
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

    assert intersection.check("shell") == "deny"


class TestConfigLoading:
    """Loading rules from XBotv2.config."""

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
                        "tool": "(?:read|edit|path|search)",
                        "params": {"path": "/tmp/.*"},
                    },
                ],
            }
        )
        assert ps.check("read", {"path": "/tmp/ok.txt"}) == "allow"
        assert ps.check("read", {"path": "/etc/bad"}) == ps.default_decision
        assert ps.check("read", {}) == ps.default_decision

    def test_workspace_scope_checks_every_path(self, tmp_path):
        workspace = tmp_path / "workspace"
        outside = tmp_path / "outside"
        workspace.mkdir()
        outside.mkdir()
        (workspace / "link").symlink_to(outside, target_is_directory=True)
        permissions = PermissionSystem(
            {
                "allow": [{
                    "tool": "(?:read|edit|path|search)",
                    "paths": "${workspace}",
                }],
            },
            variables=RuntimeVariables({"workspace": workspace}),
        )

        assert permissions.check(
            "edit", {"path": "notes.md", "mode": "write"}
        ) == "allow"
        assert permissions.check(
            "edit", {"path": "../outside/notes.md", "mode": "write"}
        ) == "ask"
        assert permissions.check(
            "edit", {"path": "link/notes.md", "mode": "write"}
        ) == "ask"
        assert permissions.check("path", {
            "operation": "move",
            "source": "old.md",
            "destination": "archive/new.md",
        }) == "allow"
        assert permissions.check("path", {
            "operation": "move",
            "source": "old.md",
            "destination": str(outside / "new.md"),
        }) == "ask"

    def test_path_scope_supports_regex_and_workspace_expansion(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        permissions = PermissionSystem(
            {"allow": [{
                "tool": "edit",
                "paths": "${workspace}/generated/.*",
            }]},
            variables=RuntimeVariables({"workspace": workspace}),
        )

        assert permissions.check(
            "edit", {"path": "generated/result.txt", "mode": "write"}
        ) == "allow"
        assert permissions.check(
            "edit", {"path": "src/result.txt", "mode": "write"}
        ) == "ask"

        absolute = PermissionSystem({
            "allow": [{
                "tool": "read",
                "paths": re.escape(str(tmp_path)) + "/allowed/.*",
            }],
        })
        assert absolute.check(
            "read", {"path": str(tmp_path / "allowed/file.txt")}
        ) == "allow"

    def test_unknown_path_variable_is_rejected(self):
        with pytest.raises(ValueError, match="Unknown runtime variable"):
            PermissionSystem({
                "allow": [{"tool": "edit", "paths": "${unknown}"}],
            })

    def test_invalid_path_regex_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid permission path regex"):
            PermissionSystem({
                "allow": [{"tool": "edit", "paths": "["}],
            })

    def test_shipped_policy_allows_internal_and_workspace_tools(self, tmp_path):
        # xcore.yaml is the unified configuration document.
        tree = yaml.safe_load(
            Path("XBotv2/xcore.yaml").read_text(encoding="utf-8")
        )
        entry = next(item for item in tree if item.get("id") == "permissions")
        config = entry["config"]["permissions"]
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        permissions = PermissionSystem(
            config=config,
            variables=RuntimeVariables({"workspace": workspace}),
        )

        for tool_name in ("shell", "update_todos"):
            assert permissions.check(tool_name, {}) == "allow"

        from XBotv2.sandbox.filesystem_ops import resolve_operation
        for tool in FILESYSTEM_TOOLS:
            operation = resolve_operation(tool.name, _shipped_args(tool.name))
            fields = PATH_ACCESS[operation]
            args = {field: "notes.md" for field, _access in fields}
            assert permissions.check(tool.name, args) == "allow"

            write_fields = [field for field, access in fields if access == "write"]
            if write_fields:
                args[write_fields[0]] = str(tmp_path / "outside.md")
                assert permissions.check(tool.name, args) == "ask"

        assert permissions.check("unknown_tool", {}) == "ask"


def _shipped_args(tool_name: str) -> dict[str, Any]:
    """Default args for shipped-policy checks of merged tools."""
    if tool_name == "edit":
        return {"path": "notes.md", "mode": "write"}
    if tool_name == "path":
        return {"operation": "mkdir", "path": "notes.md"}
    return {"path": "notes.md"}
