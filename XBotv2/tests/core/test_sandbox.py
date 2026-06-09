"""Tests for SandboxPolicy with BubblewrapBackend."""

from pathlib import Path

import pytest

from xbotv2.tools.sandbox import SandboxPolicy, SandboxResourceRule
from xbotv2.tools.sandbox_bwrap import _build_args


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


class TestBubblewrapBuildArgs:
    def test_network_true_uses_share_net(self, temp_workspace):
        args = _build_args([], network=True, cwd=str(temp_workspace))
        assert "--share-net" in args
        assert "--unshare-net" not in args

    def test_network_false_uses_unshare_net(self, temp_workspace):
        args = _build_args([], network=False, cwd=str(temp_workspace))
        assert "--unshare-net" in args
        assert "--share-net" not in args

    def test_etc_dns_files_are_bound(self, temp_workspace):
        args = _build_args([], network=True, cwd=str(temp_workspace))
        # resolv.conf and nsswitch.conf must be bind-mounted so
        # DNS resolution works inside the sandbox.
        assert "--ro-bind-try" in args
        assert "/etc/resolv.conf" in args
        assert "/etc/nsswitch.conf" in args
        # TLS roots too — curl on HTTPS endpoints.
        assert "/etc/ssl/certs" in args
