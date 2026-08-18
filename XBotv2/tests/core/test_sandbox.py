"""Tests for SandboxPolicy with BubblewrapBackend."""

import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from XBotv2.coretools.shell import run_shell_command
from XBotv2.sandbox.policy import SandboxPolicy
from XBotv2.sandbox.bwrap import _build_args
from XBotv2.core.variables import RuntimeVariables

class TestSandboxPolicyBasics:
    def test_default_enabled(self, temp_workspace):
        policy = SandboxPolicy(workspace_root=str(temp_workspace))
        assert policy.enabled is True

    @pytest.mark.parametrize("enabled", [False, True])
    def test_describe_reports_enabled_state(self, temp_workspace, enabled):
        policy = SandboxPolicy(enabled=enabled, workspace_root=str(temp_workspace))
        assert ("enabled" if enabled else "disabled") in policy.describe().lower()


class TestResourcePathResolution:
    def test_resolve_resource_path(self, temp_workspace):
        policy = SandboxPolicy(
            workspace_root=str(temp_workspace),
            data_root=str(temp_workspace / "data"),
        )
        resolved = policy.resolve_resource_path("skills/test.md")
        assert Path(resolved) == temp_workspace / "data" / "skills" / "test.md"

    def test_resource_path_uses_shared_runtime_variables(self, tmp_path):
        plugin_states = tmp_path / "session" / "plugin_states"
        variables = RuntimeVariables({
            "workspace": tmp_path / "workspace",
            "data_dir": tmp_path / "data",
            "plugin_states": plugin_states,
        })
        policy = SandboxPolicy(
            config={
                "resources": [
                    {"path": "${plugin_states}", "access": "readonly"},
                ],
            },
            workspace_root=tmp_path / "workspace",
            data_root=tmp_path / "data",
            variables=variables,
        )

        assert policy.to_dict()["resources"] == [{
            "path": str(plugin_states.resolve()),
            "access": "readonly",
        }]

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

    def test_session_write_cannot_be_enabled_by_a_resource_rule(self, tmp_path):
        workspace = tmp_path / "workspace"
        session_root = tmp_path / "data" / "sessions" / "s" / "state"
        workspace.mkdir()
        session_root.mkdir(parents=True)
        policy = SandboxPolicy(
            config={
                "resources": [
                    {"path": str(session_root), "access": "readwrite"},
                ],
            },
            workspace_root=workspace,
            session_root=session_root,
        )

        assert policy.check_filesystem_access(
            "write", {"path": "session/state.txt"}
        )[0]["decision"] == "deny"

    def test_session_symlink_cannot_redirect_a_write(self, tmp_path):
        workspace = tmp_path / "workspace"
        session_root = tmp_path / "data" / "sessions" / "s" / "state"
        external = tmp_path / "external.txt"
        workspace.mkdir()
        session_root.mkdir(parents=True)
        external.write_text("external", encoding="utf-8")
        (session_root / "link.txt").symlink_to(external)
        policy = SandboxPolicy(
            config={"external_write": "allow"},
            workspace_root=workspace,
            session_root=session_root,
        )

        assert policy.check_filesystem_access(
            "write", {"path": "session/link.txt"}
        )[0]["decision"] == "deny"


class TestBubblewrapBuildArgs:
    def test_network_true_uses_share_net(self, temp_workspace):
        args = _build_args([], network=True, cwd=str(temp_workspace))
        assert "--share-net" in args
        assert "--unshare-net" not in args

    def test_network_false_uses_unshare_net(self, temp_workspace):
        args = _build_args([], network=False, cwd=str(temp_workspace))
        assert "--unshare-net" in args
        assert "--share-net" not in args

    def test_root_is_readonly_and_tmp_is_writable(self, temp_workspace):
        args = _build_args([], network=True, cwd=str(temp_workspace))
        root_bind = args.index("--ro-bind")
        assert args[root_bind : root_bind + 3] == ["--ro-bind", "/", "/"]
        tmp_bind = args.index("--bind")
        assert args[tmp_bind : tmp_bind + 3] == ["--bind", "/tmp", "/tmp"]


class TestBubblewrapCapabilities:
    @pytest.mark.asyncio
    async def test_real_session_mount_and_shell(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XBOT_SANDBOX_TEST_ENV", "from-host")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        source = workspace / "search.txt"
        source.write_text("first\nsearch target\n", encoding="utf-8")
        session_root = tmp_path / ".data" / "sessions" / "s" / "state"
        cached_path = session_root / "artifacts" / "tool_results" / "cached.txt"
        cached_path.parent.mkdir(parents=True)
        cached_path.write_text("cached", encoding="utf-8")
        policy = SandboxPolicy(
            enabled=True,
            workspace_root=workspace,
            data_root=tmp_path / ".data",
            session_root=session_root,
        )
        if not policy.backend_available:
            pytest.skip("bubblewrap is not installed")

        cached = json.loads(await policy.filesystem(
            "read",
            {"path": "session/artifacts/tool_results/cached.txt"},
        ))
        searched = json.loads(await policy.filesystem(
            "search",
            {"path": str(source), "pattern": "target"},
        ))
        searched_directory = json.loads(await policy.filesystem(
            "search",
            {"path": str(workspace), "pattern": "target"},
        ))

        assert cached["content"] == "cached"
        assert searched["matches"][0]["line"] == 2
        assert searched_directory["matches"][0]["path"] == "search.txt"
        shell = os.environ["SHELL"]
        assert await policy.run_shell(
            "printf sandbox-ok", shell=shell
        ) == "sandbox-ok"
        assert (
            await policy.run_shell(
                'printf %s "$XBOT_SANDBOX_TEST_ENV"', shell=shell
            )
            == "from-host"
        )
        host_shell = await run_shell_command('printf %s "$0"')
        sandbox_shell = await policy.run_shell('printf %s "$0"', shell=shell)
        assert Path(host_shell).resolve() == Path(shell).resolve()
        assert Path(sandbox_shell).resolve() == Path(shell).resolve()
        assert (
            await policy.run_shell(
                f"{shlex.quote(sys.executable)} "
                "-c 'import sys; print(sys.prefix)'",
                shell=shell,
            )
        ).strip() == str(Path(sys.prefix).resolve())


class TestSandboxPolicySerialisation:
    def test_to_dict_round_trip(self, temp_workspace):
        """Serialized policy reconstructs the same live configuration."""
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
        assert SandboxPolicy(
            config=d, workspace_root=str(temp_workspace)
        ).to_dict() == d

    def test_update_from_config_preserves_untouched_keys(self, temp_workspace):
        policy = SandboxPolicy(
            config={"enabled": True, "network": True},
            workspace_root=str(temp_workspace),
        )
        policy.update_from_config({"network": False})
        assert policy.enabled is True
        assert policy.network is False

    def test_external_read_default_values(self, temp_workspace):
        policy = SandboxPolicy(workspace_root=str(temp_workspace))
        assert policy.external_read == "readonly"
        assert policy.external_write == "deny"
        assert policy.workspace_read == "allow"
        assert policy.workspace_write == "allow"
