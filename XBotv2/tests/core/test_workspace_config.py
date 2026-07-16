"""Startup-only workspace configuration tests."""

import pytest
import yaml

from xbotv2.api import RuntimePaths
from xbotv2.config.loader import load_system_config


def _write_yaml(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value), encoding="utf-8")


def test_workspace_files_overlay_policy_plugins_and_hooks(
    temp_data_dir, temp_workspace
):
    (temp_workspace / "extensions").mkdir()
    _write_yaml(
        temp_data_dir / "config" / "system.yaml",
        {
            "plugins": {"sample": {"origin": "global"}},
            "hooks": [{"stage": "on_turn_start", "target": "global:hook"}],
        },
    )
    _write_yaml(
        temp_data_dir / "config" / "permissions.yaml",
        {"ask": [{"tool": ".*"}]},
    )
    _write_yaml(
        temp_workspace / ".xbot" / "policy.yaml",
        {"permissions": {"allow": [{"tool": "filesystem_read"}]}},
    )
    _write_yaml(
        temp_workspace / ".xbot" / "plugins.yaml",
        {
            "paths": ["extensions"],
            "plugins": {
                "sample": {"config": {"origin": "workspace"}},
                "disabled": {"enabled": False},
            },
        },
    )
    _write_yaml(
        temp_workspace / ".xbot" / "hooks.yaml",
        {"hooks": [{"stage": "on_session_init", "target": "local:hook"}]},
    )

    config = load_system_config(
        RuntimePaths.from_data_dir(temp_data_dir), temp_workspace
    )

    assert config.permissions["allow"] == [{"tool": "filesystem_read"}]
    assert config.permissions["ask"] == [{"tool": ".*"}]
    assert config.plugins["sample"] == {"origin": "workspace"}
    assert config.disabled_plugins == ["disabled"]
    assert config.plugin_paths == [str(temp_workspace / "extensions")]
    assert [hook.target for hook in config.hooks] == ["local:hook"]
    assert config.hooks[0].base_dir == temp_workspace / ".xbot"


def test_workspace_plugin_path_cannot_escape_workspace(
    temp_data_dir, temp_workspace
):
    _write_yaml(
        temp_workspace / ".xbot" / "plugins.yaml",
        {"paths": ["../external"]},
    )

    with pytest.raises(ValueError, match="inside the workspace"):
        load_system_config(RuntimePaths.from_data_dir(temp_data_dir), temp_workspace)
