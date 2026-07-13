"""Tests for SandboxPolicy with BubblewrapBackend."""

import json
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

    @pytest.mark.asyncio
    async def test_capability_returns_backend_stdout(self, temp_workspace):
        class Backend:
            async def run(self, *args, **kwargs):
                return "result\n"

        policy = SandboxPolicy(enabled=True, workspace_root=str(temp_workspace))
        policy._backend = Backend()

        assert await policy.run_shell("echo result") == "result\n"

    @pytest.mark.asyncio
    async def test_capability_raises_on_backend_failure(self, temp_workspace):
        class Backend:
            async def run(self, *args, **kwargs):
                raise RuntimeError("Sandbox command failed with exit code 2: denied")

        policy = SandboxPolicy(enabled=True, workspace_root=str(temp_workspace))
        policy._backend = Backend()

        with pytest.raises(RuntimeError, match="exit code 2: denied"):
            await policy.run_shell("false")


class TestResourcePathResolution:
    def test_resolve_resource_path(self, temp_workspace):
        policy = SandboxPolicy(
            workspace_root=str(temp_workspace),
            data_root=str(temp_workspace / "data"),
        )
        resolved = policy.resolve_resource_path("skills/test.md")
        assert str(temp_workspace / "data" / "skills" / "test.md") in resolved

    def test_session_read_path_is_limited_to_current_session(self, tmp_path):
        workspace = tmp_path / "workspace"
        session_root = tmp_path / "data" / "sessions" / "s" / "state"
        workspace.mkdir()
        policy = SandboxPolicy(
            workspace_root=workspace,
            data_root=tmp_path / "data",
            session_root=session_root,
        )

        assert policy.resolve_read_path("session/artifacts/tool_results/cached.txt") == (
            session_root / "artifacts" / "tool_results" / "cached.txt"
        ).resolve()
        assert policy.resolve_read_path("session/../outside.txt") == (
            workspace / "session" / "../outside.txt"
        ).resolve()


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


class TestBubblewrapCapabilities:
    @pytest.mark.asyncio
    async def test_real_file_and_shell_capabilities(self, tmp_path):
        session_root = tmp_path / ".data" / "sessions" / "s" / "state"
        policy = SandboxPolicy(
            enabled=True,
            workspace_root=tmp_path,
            data_root=tmp_path / ".data",
            session_root=session_root,
        )
        if not policy.backend_available:
            pytest.skip("bubblewrap is not installed")

        (tmp_path / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        readonly_path = tmp_path / ".data" / "readonly.txt"
        readonly_path.parent.mkdir()
        readonly_path.write_text("before", encoding="utf-8")
        cached_path = session_root / "artifacts" / "tool_results" / "cached.txt"
        cached_path.parent.mkdir(parents=True)
        cached_path.write_text("cached", encoding="utf-8")

        read_data = json.loads(await policy.read_file("sample.txt"))
        readonly_data = json.loads(
            await policy.read_file(str(readonly_path), offset=0, limit=1)
        )
        cached_data = json.loads(
            await policy.read_file("session/artifacts/tool_results/cached.txt")
        )
        cached_list = json.loads(
            await policy.list_dir("session/artifacts", recursive=True)
        )
        cached_search = json.loads(
            await policy.search_text("cached", "session/artifacts")
        )
        missing_data = json.loads(await policy.read_file("missing.txt"))
        write_data = json.loads(await policy.write_file("created.txt", "created"))
        write_error = json.loads(await policy.write_file(str(readonly_path), "after"))
        search_data = json.loads(await policy.search_text("alpha"))
        list_data = json.loads(await policy.list_dir(".", recursive=True))

        assert read_data["content"] == "alpha\nbeta"
        assert readonly_data["content"] == "before"
        assert readonly_data["returned_lines"] == 1
        assert cached_data["content"] == "cached"
        assert cached_list["entries"][0]["relative_path"] == "tool_results"
        assert cached_search["matches"] == ["tool_results/cached.txt:1:cached"]
        assert missing_data["ok"] is False
        assert missing_data["error"]["code"] == "file_not_found"
        assert write_data["ok"] is True
        assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "created"
        assert write_error["ok"] is False
        assert write_error["error"]["code"] == "write_failed"
        assert readonly_path.read_text(encoding="utf-8") == "before"
        assert search_data["match_count"] == 1
        assert list_data["entry_count"] >= 1
        assert await policy.run_shell("printf sandbox-ok") == "sandbox-ok"


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
