"""Generic plugin configuration catalog behavior."""

from __future__ import annotations

import pytest

from XBotv2.config.contracts import PatchPluginConfig
from XBotv2.config.plugin_catalog import (
    PluginConfigConflict,
    plugin_config_catalog,
    update_plugin_config,
)
from XBotv2.config.seed import ensure_initial_config
from XBotv2.core.paths import RuntimePaths


def test_catalog_exposes_declared_schema_without_plugin_specific_rules(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_initial_config(paths)

    catalog = plugin_config_catalog(paths, workspace, "global")
    compact = next(item for item in catalog.plugins if item.plugin_id == "compact")

    assert compact.editable is True
    assert compact.config_schema == {
        "type": "object",
        "properties": {
            "automatic": {"type": "boolean"},
            "output_reservation": {"type": "number"},
            "trigger_ratio": {"type": "number"},
            "keep_recent_turns": {"type": "number"},
            "summary_max_chars": {"type": "number"},
        },
        "additionalProperties": True,
    }
    llm = next(item for item in catalog.plugins if item.plugin_id == "llm")
    assert llm.editable is False
    assert "does not declare" in llm.unavailable_reason


def test_update_uses_scope_revision_and_validates_against_lower_layer(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_initial_config(paths)

    global_catalog = plugin_config_catalog(paths, workspace, "global")
    updated_global = update_plugin_config(
        paths,
        workspace,
        "compact",
        PatchPluginConfig(
            scope="global",
            revision=global_catalog.revision,
            config={"automatic": False},
        ),
    )
    compact = next(item for item in updated_global.plugins if item.plugin_id == "compact")
    assert compact.scope_config == {"automatic": False}
    assert compact.effective_config == {"automatic": False}

    workspace_catalog = plugin_config_catalog(paths, workspace, "workspace")
    updated_workspace = update_plugin_config(
        paths,
        workspace,
        "compact",
        PatchPluginConfig(
            scope="workspace",
            revision=workspace_catalog.revision,
            config={"keep_recent_turns": 2},
        ),
    )
    compact = next(item for item in updated_workspace.plugins if item.plugin_id == "compact")
    assert compact.scope_config == {"keep_recent_turns": 2}
    assert compact.effective_config == {
        "automatic": False,
        "keep_recent_turns": 2,
    }

    with pytest.raises(PluginConfigConflict):
        update_plugin_config(
            paths,
            workspace,
            "compact",
            PatchPluginConfig(
                scope="workspace",
                revision=workspace_catalog.revision,
                config={},
            ),
        )
