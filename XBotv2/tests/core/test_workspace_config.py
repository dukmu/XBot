"""Unified configuration overlay behavior."""

import pytest
import yaml

from XBotv2.core import RuntimePaths
from XBotv2.config.loader import load_plugin_tree


def _write_yaml(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value), encoding="utf-8")


def test_plugin_layers_resolve_to_one_generic_tree(temp_data_dir, temp_workspace):
    paths = RuntimePaths.from_data_dir(temp_data_dir)
    (temp_workspace / ".xbot" / "plugins").mkdir(parents=True)
    _write_yaml(paths.config_dir / "plugins.yaml", [{
        "id": "llm", "config": {"default_provider": "global"},
    }, {
        "id": "permissions",
        "config": {
            "rules": [{"tool_pattern": "todo", "decision": "allow"}],
            "default_decision": "ask",
        },
    }])
    _write_yaml(temp_workspace / ".xbot" / "plugins.yaml", [{
        "id": "llm", "config": {"default_provider": "workspace"},
    }])
    _write_yaml(paths.session("session").config_file, {"plugins": [{
        "id": "llm", "config": {"default_provider": "session"},
    }]})

    tree = load_plugin_tree(paths, temp_workspace, "session")
    entries = {entry.id: entry for entry in tree.entries}

    assert entries["llm"].config["default_provider"] == "session"
    assert entries["permissions"].config["rules"][0]["tool_pattern"] == "todo"


def test_session_config_rejects_aggregate_document(temp_data_dir, temp_workspace):
    paths = RuntimePaths.from_data_dir(temp_data_dir)
    _write_yaml(paths.session("session").config_file, {"provider": "legacy"})

    with pytest.raises(ValueError, match="unknown plugin document fields"):
        load_plugin_tree(paths, temp_workspace, "session")


def test_config_rejects_invalid_cache_policy_and_removed_coretools_policy(
    temp_data_dir, temp_workspace
):
    paths = RuntimePaths.from_data_dir(temp_data_dir)
    _write_yaml(paths.config_dir / "plugins.yaml", [{
        "id": "content_cache",
        "config": {
            "threshold_chars": 100,
            "preview_chars": 101,
        },
    }])
    with pytest.raises(ValueError, match="preview_chars"):
        load_plugin_tree(paths, temp_workspace)

    _write_yaml(paths.config_dir / "plugins.yaml", [{
        "id": "coretools",
        "config": {"tool_results": {"cache_threshold_chars": 100}},
    }])
    with pytest.raises(ValueError, match="tool_results"):
        load_plugin_tree(paths, temp_workspace)

    _write_yaml(paths.config_dir / "plugins.yaml", [{"agent_name": "legacy"}])
    with pytest.raises(ValueError, match="agent_name"):
        load_plugin_tree(paths, temp_workspace)
