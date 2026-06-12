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


class TestSandboxPolicySerialisation:
    def test_to_dict_round_trip(self, temp_workspace):
        """to_dict() output reconstructs an identical policy via update_from_config."""
        policy = SandboxPolicy(
            config={
                "enabled": True,
                "network": False,
                "external_read": "ask",
                "external_write": "deny",
                "workspace_read": "allow",
                "workspace_write": "allow",
                "resources": [
                    {"path": "/dev/null", "access": "readonly"},
                ],
            },
            workspace_root=str(temp_workspace),
        )
        d = policy.to_dict()
        assert d["enabled"] is True
        assert d["network"] is False
        assert d["external_read"] == "ask"
        assert d["external_write"] == "deny"
        assert d["workspace_read"] == "allow"
        assert d["workspace_write"] == "allow"
        assert len(d["resources"]) >= 1

        policy2 = SandboxPolicy(
            config=d,
            workspace_root=str(temp_workspace),
        )
        assert policy2.to_dict() == d

    def test_save_and_load_from_file(self, temp_workspace, tmp_path):
        """save() writes YAML; another SandboxPolicy can read it back."""
        policy = SandboxPolicy(
            config={
                "enabled": True,
                "network": True,
                "external_read": "readonly",
                "external_write": "deny",
                "workspace_read": "allow",
                "workspace_write": "allow",
                "resources": [{"path": "/tmp", "access": "readwrite"}],
            },
            workspace_root=str(temp_workspace),
        )
        path = tmp_path / "sandbox.yaml"
        policy.save(path)
        assert path.exists()

        from xbotv2.config.loader import load_yaml
        data = load_yaml(path)
        policy2 = SandboxPolicy(
            config=data,
            workspace_root=str(temp_workspace),
        )
        assert policy2.enabled is True
        assert policy2.network is True
        assert policy2.external_read == "readonly"
        assert policy2.external_write == "deny"
        assert policy2.to_dict() == policy.to_dict()

    def test_update_from_config_changes_network(self, temp_workspace):
        policy = SandboxPolicy(
            config={"network": True},
            workspace_root=str(temp_workspace),
        )
        assert policy.network is True
        policy.update_from_config({"network": False})
        assert policy.network is False

    def test_update_from_config_preserves_untouched_keys(self, temp_workspace):
        policy = SandboxPolicy(
            config={"enabled": True, "network": True},
            workspace_root=str(temp_workspace),
        )
        policy.update_from_config({"network": False})
        assert policy.enabled is True
        assert policy.network is False

    def test_to_dict_excludes_implicit_workspace_data_rules(self, temp_workspace):
        policy = SandboxPolicy(
            config={
                "resources": [{"path": "/dev/null", "access": "readonly"}],
            },
            workspace_root=str(temp_workspace),
        )
        d = policy.to_dict()
        paths = [r["path"] for r in d.get("resources", [])]
        assert str(temp_workspace) not in paths

    def test_external_read_default_values(self, temp_workspace):
        policy = SandboxPolicy(workspace_root=str(temp_workspace))
        assert policy.external_read == "readonly"
        assert policy.external_write == "deny"
        assert policy.workspace_read == "allow"
        assert policy.workspace_write == "allow"
