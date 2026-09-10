"""Schema-driven plugin configuration discovery and layered overlay writes."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, JsonValue

from XBotv2.loader import (
    plugin_config_json_schema,
    plugin_config_schema,
    resolve_agent_tree,
    validate_plugin_config,
)
from XBotv2.config.contracts import (
    PatchPluginConfig,
    PluginConfigCatalog,
    PluginConfigConflict,
    PluginConfigDescriptor,
    PluginConfigScope,
    PluginConfigUnavailable,
)
from XBotv2.core.filesystem.atomic import write_text_atomic
from XBotv2.core.paths import RuntimePaths
from XBotv2.loader.contracts import PluginEntry, PluginOverlay


def plugin_config_catalog(
    paths: RuntimePaths,
    workspace_root: Path | str,
    scope: PluginConfigScope,
    session_id: str | None = None,
) -> PluginConfigCatalog:
    """Describe resolved plugin declarations without mounting their lifecycles."""
    workspace = Path(workspace_root).resolve()
    overlay_path = _overlay_path(paths, workspace, scope, session_id)
    scope_configs = _overlay_configs(overlay_path)
    tree = resolve_agent_tree(
        paths=paths,
        workspace_root=workspace,
        is_subagent=False,
        no_plugins=False,
        plugin_dirs=None,
        extra_plugins=None,
        session_id=session_id if scope == "session" else None,
        include_global=True,
        include_workspace=scope in {"workspace", "session"},
        include_session=scope == "session",
    )
    plugins = [
        _descriptor(entry, scope_configs.get(entry.id, {}))
        for entry in tree.entries
    ]
    return PluginConfigCatalog(
        scope=scope,
        workspace_root=str(workspace),
        revision=_revision(overlay_path),
        applies_to="current_session" if scope == "session" else "new_sessions",
        plugins=plugins,
    )


def update_plugin_config(
    paths: RuntimePaths,
    workspace_root: Path | str,
    plugin_id: str,
    patch: PatchPluginConfig,
    session_id: str | None = None,
) -> PluginConfigCatalog:
    """Validate and replace one layer's config object for a declared plugin."""
    workspace = Path(workspace_root).resolve()
    path = _overlay_path(paths, workspace, patch.scope, session_id)
    actual_revision = _revision(path)
    if patch.revision != actual_revision:
        raise PluginConfigConflict(
            "Plugin configuration changed on disk; reload before saving."
        )

    lower_tree = resolve_agent_tree(
        paths=paths,
        workspace_root=workspace,
        is_subagent=False,
        no_plugins=False,
        plugin_dirs=None,
        extra_plugins=None,
        include_global=patch.scope != "global",
        include_workspace=patch.scope == "session",
        include_session=False,
    )
    entry = _entry(lower_tree.entries, plugin_id)
    schema = _declared_schema(entry)
    effective = _merge_config(entry.config, patch.config)
    try:
        validate_plugin_config(schema, effective)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    document = _overlay_document(path)
    rows = _document_rows(document)
    _replace_config(rows, plugin_id, patch.config)
    PluginOverlay.parse(document)
    write_text_atomic(
        path,
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
    )
    return plugin_config_catalog(paths, workspace, patch.scope, session_id)


def _descriptor(
    entry: PluginEntry,
    scope_config: dict[str, JsonValue],
) -> PluginConfigDescriptor:
    try:
        schema = _declared_schema(entry)
    except (ImportError, TypeError, ValueError) as exc:
        return PluginConfigDescriptor(
            plugin_id=entry.id,
            name=entry.name,
            editable=False,
            scope_config=scope_config,
            effective_config=entry.config,
            unavailable_reason=str(exc),
        )
    return PluginConfigDescriptor(
        plugin_id=entry.id,
        name=entry.name,
        editable=True,
        config_schema=plugin_config_json_schema(schema),
        scope_config=scope_config,
        effective_config=entry.config,
    )


def _declared_schema(entry: PluginEntry) -> object:
    schema = plugin_config_schema(entry)
    if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        raise PluginConfigUnavailable(
            "This plugin does not declare a Pydantic Config model."
        )
    return schema


def _entry(entries: list[PluginEntry], plugin_id: str) -> PluginEntry:
    for entry in entries:
        if entry.id == plugin_id:
            return entry
    raise PluginConfigUnavailable(f"Unknown plugin id: {plugin_id}")


def _overlay_path(
    paths: RuntimePaths,
    workspace: Path,
    scope: PluginConfigScope,
    session_id: str | None,
) -> Path:
    if scope == "global":
        return paths.config_dir / "plugins.yaml"
    if scope == "workspace":
        return workspace / ".xbot" / "plugins.yaml"
    if not session_id:
        raise ValueError("session scope requires a session id")
    return paths.session(session_id).config_file


def _revision(path: Path) -> str:
    content = path.read_bytes() if path.exists() else b""
    return hashlib.sha256(content).hexdigest()


def _overlay_configs(path: Path) -> dict[str, dict[str, JsonValue]]:
    document = _overlay_document(path)
    configs: dict[str, dict[str, JsonValue]] = {}
    for row in _document_rows(document):
        plugin_id = row.get("id", row.get("name"))
        config = row.get("config")
        if isinstance(plugin_id, str) and isinstance(config, dict):
            configs[plugin_id] = dict(config)
    return configs


def _overlay_document(path: Path) -> list[object] | dict[str, object]:
    if not path.exists():
        return []
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if value is None:
        return []
    if not isinstance(value, (list, dict)):
        raise ValueError(f"{path} must contain a plugin list or mapping")
    return value


def _document_rows(document: list[object] | dict[str, object]) -> list[dict[str, object]]:
    values: object
    if isinstance(document, list):
        values = document
    else:
        present = [key for key in ("plugins", "entries") if key in document]
        if len(present) != 1:
            raise ValueError("plugin overlay must contain exactly one of plugins or entries")
        values = document[present[0]]
    if not isinstance(values, list) or not all(isinstance(row, dict) for row in values):
        raise ValueError("plugin overlay entries must be mappings")
    return values


def _replace_config(
    rows: list[dict[str, object]],
    plugin_id: str,
    config: dict[str, JsonValue],
) -> None:
    for row in rows:
        if row.get("id", row.get("name")) == plugin_id:
            row["config"] = dict(config)
            return
    rows.append({"id": plugin_id, "config": dict(config)})


def _merge_config(
    base: Mapping[str, JsonValue],
    overlay: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        merged[key] = (
            _merge_config(current, value)
            if isinstance(current, dict) and isinstance(value, dict)
            else value
        )
    return merged


__all__ = [
    "PluginConfigConflict",
    "PluginConfigUnavailable",
    "plugin_config_catalog",
    "update_plugin_config",
]
