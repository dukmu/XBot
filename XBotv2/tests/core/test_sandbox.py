"""Tests for SandboxPolicy."""

import os
from pathlib import Path

import pytest

from xbotv2.tools.sandbox import SandboxPolicy, SandboxResourceRule


class TestSandboxResourceRule:
    """Resource rule matching."""

    def test_rule_matches_subpath(self):
        """A rule for /foo matches /foo/bar."""
        rule = SandboxResourceRule(path="/foo", access="readwrite")
        assert rule.matches("/foo/bar") is True
        assert rule.matches("/foo/bar/baz") is True

    def test_rule_matches_exact(self):
        """A rule for /foo matches /foo itself."""
        rule = SandboxResourceRule(path="/foo")
        assert rule.matches("/foo") is True

    def test_rule_does_not_match_sibling(self):
        """A rule for /foo does not match /bar."""
        rule = SandboxResourceRule(path="/foo")
        assert rule.matches("/bar") is False

    def test_rule_does_not_match_parent(self):
        """A rule for /foo/bar does not match /foo."""
        rule = SandboxResourceRule(path="/foo/bar")
        assert rule.matches("/foo") is False


class TestSandboxPolicyBasics:
    """Policy creation and description."""

    def test_default_disabled(self, temp_workspace):
        """Sandbox is disabled by default."""
        policy = SandboxPolicy(
            enabled=False,
            workspace_root=str(temp_workspace),
        )
        assert policy.enabled is False

    def test_describe_disabled(self, temp_workspace):
        """Description reflects disabled state."""
        policy = SandboxPolicy(
            enabled=False,
            workspace_root=str(temp_workspace),
        )
        desc = policy.describe()
        assert "disabled" in desc.lower()

    def test_describe_enabled(self, temp_workspace):
        """Description reflects enabled state."""
        policy = SandboxPolicy(
            enabled=True,
            workspace_root=str(temp_workspace),
        )
        desc = policy.describe()
        assert "enabled" in desc.lower()

    def test_config_loading(self, temp_workspace):
        """Config dict loads resource rules."""
        policy = SandboxPolicy(
            config={
                "enabled": True,
                "resources": [
                    {"path": "/data", "access": "readonly"},
                ],
            },
            workspace_root=str(temp_workspace),
        )
        assert policy.enabled is True


class TestToolGuard:
    """Tool call guard decisions."""

    def test_host_tools_always_allowed(self, temp_workspace):
        """Host-mode tools bypass sandbox checks."""
        policy = SandboxPolicy(
            enabled=True,
            workspace_root=str(temp_workspace),
        )
        allowed, reason = policy.guard_tool_call(
            "some_tool", {"path": "/etc/passwd"}, "host"
        )
        assert allowed is True
        assert reason == ""

    def test_sandboxed_tool_denied_outside_workspace(self, temp_workspace):
        """When sandbox enabled, workspace-relative paths outside workspace denied."""
        ws = Path(temp_workspace)
        policy = SandboxPolicy(
            enabled=True,
            workspace_root=str(ws),
        )
        # An absolute path outside workspace
        outside = "/etc/passwd"
        allowed, reason = policy.guard_tool_call(
            "filesystem_read", {"path": outside}, "sandboxed"
        )
        assert allowed is False
        assert "denied" in reason.lower() or "escape" in reason.lower()

    def test_disabled_sandbox_allows_workspace(self, temp_workspace):
        """When disabled, workspace paths are allowed."""
        ws = Path(temp_workspace)
        (ws / "test.txt").write_text("hello")
        policy = SandboxPolicy(
            enabled=False,
            workspace_root=str(ws),
        )
        allowed, reason = policy.guard_tool_call(
            "filesystem_read", {"path": "test.txt"}, "sandboxed"
        )
        assert allowed is True

    def test_disabled_sandbox_denies_absolute_outside(self, temp_workspace):
        """When disabled, absolute paths outside workspace are denied."""
        ws = Path(temp_workspace)
        policy = SandboxPolicy(
            enabled=False,
            workspace_root=str(ws),
        )
        allowed, reason = policy.guard_tool_call(
            "filesystem_read", {"path": "/etc/shadow"}, "sandboxed"
        )
        assert allowed is False

    def test_symlink_escape_detection(self, temp_workspace):
        """Symlinks pointing outside workspace are detected."""
        ws = Path(temp_workspace)
        # Create a symlink that points outside
        symlink_path = ws / "escape_link"
        symlink_path.symlink_to("/etc/passwd")

        policy = SandboxPolicy(
            enabled=True,
            workspace_root=str(ws),
        )
        allowed, reason = policy.guard_tool_call(
            "filesystem_read", {"path": "escape_link"}, "sandboxed"
        )
        assert allowed is False
        assert "escape" in reason.lower() or "denied" in reason.lower()

        # Cleanup
        symlink_path.unlink()

    def test_ask_access_fails_closed_until_tool_replay_exists(self, temp_workspace):
        """Ask access does not implicitly allow sandboxed paths."""
        ws = Path(temp_workspace)
        gated = ws / "gated"
        gated.mkdir()
        policy = SandboxPolicy(
            config={
                "enabled": True,
                "resources": [
                    {"path": str(gated), "access": "ask"},
                ],
            },
            workspace_root=str(ws),
        )

        allowed, reason = policy.guard_tool_call(
            "filesystem_read", {"path": str(gated / "file.txt")}, "sandboxed"
        )

        assert allowed is False
        assert "permission.response can record the decision" in reason
        assert "fails closed" in reason


class TestPathResolution:
    """Path resolution against workspace/data roots."""

    def test_resolve_relative_to_workspace(self, temp_workspace):
        """Relative paths resolve against workspace."""
        ws = Path(temp_workspace)
        policy = SandboxPolicy(workspace_root=str(ws))
        resolved = policy.resolve_tool_path("subdir/file.txt")
        assert resolved == str((ws / "subdir" / "file.txt").resolve())

    def test_resolve_absolute_unchanged(self, temp_workspace):
        """Absolute paths stay absolute (resolved)."""
        ws = Path(temp_workspace)
        policy = SandboxPolicy(workspace_root=str(ws))
        resolved = policy.resolve_tool_path("/tmp/some_file")
        assert resolved == "/tmp/some_file"

    def test_resolve_resource_path(self, temp_workspace):
        """Resource paths resolve against data root."""
        policy = SandboxPolicy(
            workspace_root=str(temp_workspace),
            data_root=str(temp_workspace / "data"),
        )
        resolved = policy.resolve_resource_path("skills/test.md")
        assert str(temp_workspace / "data" / "skills" / "test.md") in resolved


class TestOneCallApprovals:
    """Transient path approvals."""

    def test_approve_once(self, temp_workspace):
        """One-call approval is tracked."""
        policy = SandboxPolicy(workspace_root=str(temp_workspace))
        policy.approve_once("/some/path", "read")
        assert ("/some/path", "read") in policy._one_call_approvals

    def test_clear_one_call_approvals(self, temp_workspace):
        """Clear removes all approvals."""
        policy = SandboxPolicy(workspace_root=str(temp_workspace))
        policy.approve_once("/p1", "read")
        policy.approve_once("/p2", "write")
        policy.clear_one_call_approvals()
        assert len(policy._one_call_approvals) == 0
