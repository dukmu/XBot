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
from XBotv2.compact.contracts import CompactConfig


def test_catalog_exposes_declared_schema_without_plugin_specific_rules(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_initial_config(paths)

    catalog = plugin_config_catalog(paths, workspace, "global")
    compact = next(item for item in catalog.plugins if item.plugin_id == "compact")

    assert compact.editable is True
    assert compact.config_schema == CompactConfig.model_json_schema(mode="serialization")
    llm = next(item for item in catalog.plugins if item.plugin_id == "llm")
    assert llm.editable is True
    assert llm.config_schema["properties"]["providers"]["type"] == "object"


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


def test_session_scope_uses_workspace_as_its_lower_layer(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_initial_config(paths)
    (workspace / ".xbot").mkdir()
    (workspace / ".xbot" / "plugins.yaml").write_text(
        "- id: compact\n  config:\n    automatic: false\n",
        encoding="utf-8",
    )

    catalog = plugin_config_catalog(paths, workspace, "session", "session-1")
    # Session-scope writes are read live by settings readers, but mounted
    # plugins keep their config until the next mount, so the catalog reports
    # the conservative value instead of promising current-session behavior.
    assert catalog.applies_to == "new_sessions"
    compact = next(item for item in catalog.plugins if item.plugin_id == "compact")
    assert compact.effective_config["automatic"] is False

    updated = update_plugin_config(
        paths,
        workspace,
        "compact",
        PatchPluginConfig(
            scope="session",
            revision=catalog.revision,
            config={"keep_recent_turns": 2},
        ),
        session_id="session-1",
    )
    compact = next(item for item in updated.plugins if item.plugin_id == "compact")
    assert compact.scope_config == {"keep_recent_turns": 2}
    assert compact.effective_config == {"automatic": False, "keep_recent_turns": 2}


def test_catalog_discovers_external_plugin_from_workspace_overlay(tmp_path, monkeypatch):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    workspace = tmp_path / "workspace"
    plugin_root = workspace / ".xbot" / "plugins"
    plugin_root.mkdir(parents=True)
    (plugin_root / "external_demo").mkdir()
    (plugin_root / "external_demo" / "plugin.py").write_text(
        "from pydantic import BaseModel, ConfigDict\n"
        "class ExternalConfig(BaseModel):\n"
        "    model_config = ConfigDict(extra='forbid')\n"
        "    enabled: bool = True\n"
        "class ExternalDemo:\n"
        "    name = 'external_demo'\n"
        "    Config = ExternalConfig\n"
        "    def apply(self, ctx, config=None):\n"
        "        return None\n"
        "plugin = ExternalDemo()\n",
        encoding="utf-8",
    )
    (workspace / ".xbot" / "config.yaml").write_text(
        "{}\n",
        encoding="utf-8",
    )
    (workspace / ".xbot" / "plugins.yaml").write_text(
        "- id: external_demo\n  name: external_demo\n  config: {}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(plugin_root))
    ensure_initial_config(paths)

    catalog = plugin_config_catalog(paths, workspace, "workspace")
    external = next(item for item in catalog.plugins if item.plugin_id == "external_demo")
    assert external.editable is True
    assert external.config_schema["type"] == "object"
    assert external.config_schema["properties"]["enabled"]["type"] == "boolean"
    assert external.config_schema["additionalProperties"] is False


def test_user_context_follows_session_overlay_writes(tmp_path):
    """The settings service reads the user identity live: a session-scope
    overlay write is visible without rebuilding the service, matching the
    freshness policy() already had."""
    from XBotv2.config.service import ConfigService
    from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG
    from xcore import Context

    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_initial_config(paths)

    service = ConfigService(
        paths,
        session_id="s",
        workspace_root=workspace,
        events=Context(),
        runtime_log=DEFAULT_RUNTIME_LOG,
    )
    # The resolved tree is the single source of the user context.
    assert service.user_context().user_name == "User"

    catalog = plugin_config_catalog(paths, workspace, "session", "s")
    update_plugin_config(
        paths,
        workspace,
        "config",
        PatchPluginConfig(
            scope="session",
            revision=catalog.revision,
            config={"user": {"user_name": "live-user", "user_id": "live-id"}},
        ),
        session_id="s",
    )

    # Same service instance: the reader re-resolves the overlay.
    refreshed = service.user_context()
    assert refreshed.user_name == "live-user"
    assert refreshed.user_id == "live-id"
