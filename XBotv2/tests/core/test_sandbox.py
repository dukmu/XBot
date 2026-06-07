"""Tests for SandboxPolicy with BubblewrapBackend."""

from pathlib import Path

import pytest

from xbotv2.tools.sandbox import SandboxPolicy, SandboxResourceRule


class TestSandboxResourceRule:
    def test_rule_matches_subpath(self):
        rule = SandboxResourceRule(path="/foo", access="readwrite")
        assert rule.matches("/foo/bar") is True
        assert rule.matches("/foo/bar/baz") is True

    def test_rule_matches_exact(self):
        rule = SandboxResourceRule(path="/foo")
        assert rule.matches("/foo") is True

    def test_rule_does_not_match_sibling(self):
        rule = SandboxResourceRule(path="/foo")
        assert rule.matches("/bar") is False

    def test_rule_does_not_match_parent(self):
        rule = SandboxResourceRule(path="/foo/bar")
        assert rule.matches("/foo") is False


class TestSandboxPolicyBasics:
    def test_default_enabled(self, temp_workspace):
        policy = SandboxPolicy(workspace_root=str(temp_workspace))
        assert policy.enabled is True

    def test_default_disabled(self, temp_workspace):
        policy = SandboxPolicy(enabled=False, workspace_root=str(temp_workspace))
        assert policy.enabled is False

    def test_describe_disabled(self, temp_workspace):
        policy = SandboxPolicy(enabled=False, workspace_root=str(temp_workspace))
        desc = policy.describe()
        assert "disabled" in desc.lower()

    def test_describe_enabled(self, temp_workspace):
        policy = SandboxPolicy(enabled=True, workspace_root=str(temp_workspace))
        desc = policy.describe()
        assert "enabled" in desc.lower()

    def test_config_loading(self, temp_workspace):
        policy = SandboxPolicy(
            config={
                "enabled": True,
                "resources": [{"path": "/data", "access": "readonly"}],
            },
            workspace_root=str(temp_workspace),
        )
        assert policy.enabled is True

    def test_backend_availability_property(self, temp_workspace):
        policy = SandboxPolicy(enabled=True, workspace_root=str(temp_workspace))
        assert isinstance(policy.backend_available, bool)


class TestResourcePathResolution:
    def test_resolve_resource_path(self, temp_workspace):
        policy = SandboxPolicy(
            workspace_root=str(temp_workspace),
            data_root=str(temp_workspace / "data"),
        )
        resolved = policy.resolve_resource_path("skills/test.md")
        assert str(temp_workspace / "data" / "skills" / "test.md") in resolved
