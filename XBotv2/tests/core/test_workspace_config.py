"""Unified configuration overlay behavior."""

import pytest
import yaml

from xbotv2.api import RuntimePaths
from xbotv2.config.loader import load_runtime_config


def _write_yaml(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value), encoding="utf-8")


def test_workspace_overrides_session_and_global_config(
    temp_data_dir, temp_workspace
):
    paths = RuntimePaths.from_data_dir(temp_data_dir)
    (temp_workspace / ".xbot" / "plugins").mkdir(parents=True)
    _write_yaml(paths.config_file, {
        "provider": "global",
        "plugins": {"sample": {"config": {"source": "global"}}},
        "permissions": {
            "allow": [{"tool": "todo"}],
            "ask": [{"tool": ".*"}],
        },
    })
    _write_yaml(paths.session("session").config_file, {
        "provider": "session",
        "plugins": {"sample": {"config": {"source": "session"}}},
    })
    _write_yaml(temp_workspace / ".xbot" / "config.yaml", {
        "provider": "workspace",
        "plugin_paths": [".xbot/plugins"],
        "workspace_tools": [{"target": "tools/example.py:TOOLS"}],
        "plugins": {
            "sample": {"config": {"source": "workspace"}},
            "disabled": {"enabled": False},
        },
        "hooks": [{"stage": "on_session_init", "target": "local:hook"}],
        "permissions": {"allow": [{"tool": "filesystem_read"}]},
    })

    config = load_runtime_config(paths, temp_workspace, "session")

    assert config.provider == "workspace"
    assert config.permissions.allow[0].tool == "filesystem_read"
    assert config.permissions.allow[1].tool == "todo"
    assert config.permissions.ask[0].tool == ".*"
    assert config.plugins["sample"].config == {"source": "workspace"}
    assert config.disabled_plugins == ["disabled"]
    assert config.plugin_paths == [str(temp_workspace / ".xbot" / "plugins")]
    assert config.workspace_tools[0].base_dir == temp_workspace / ".xbot"
    assert config.hooks[0].base_dir == temp_workspace / ".xbot"


def test_workspace_plugin_path_cannot_escape_workspace(
    temp_data_dir, temp_workspace
):
    _write_yaml(
        temp_workspace / ".xbot" / "config.yaml",
        {"plugin_paths": ["../external"]},
    )

    with pytest.raises(ValueError, match="inside the workspace"):
        load_runtime_config(RuntimePaths.from_data_dir(temp_data_dir), temp_workspace)


def test_config_rejects_unknown_fields_and_invalid_tool_result_limits(
    temp_data_dir, temp_workspace
):
    paths = RuntimePaths.from_data_dir(temp_data_dir)
    _write_yaml(paths.config_file, {
        "tool_results": {"max_inline_chars": 100, "preview_chars": 101},
    })
    with pytest.raises(ValueError, match="preview_chars"):
        load_runtime_config(paths, temp_workspace)

    _write_yaml(paths.config_file, {"agent_name": "legacy"})
    with pytest.raises(ValueError, match="agent_name"):
        load_runtime_config(paths, temp_workspace)
