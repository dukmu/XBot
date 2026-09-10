"""Permission and sandbox policy overlay/persistence helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from pydantic import JsonValue

import yaml
from XBotv2.core.paths import RuntimePaths
from XBotv2.loader.contracts import PluginOverlay


_PERMISSION_DECISIONS = ("deny", "allow", "ask")


def load_session_policy(paths: RuntimePaths, session_id: str) -> dict[str, JsonValue]:
    """Read policy fields from the canonical session plugin overlay."""
    rows = _read_rows(paths.session(session_id).config_file)
    result: dict[str, JsonValue] = {}
    for plugin_id, key in (("permissions", "permissions"), ("sandbox", "sandbox")):
        config = _plugin_config(rows, plugin_id)
        if config:
            result[key] = config
    return result


def patch_session_policy(
    *,
    paths: RuntimePaths,
    session_id: str,
    permissions: dict[str, str] | None = None,
    remove_permissions: Iterable[str] = (),
    sandbox: dict[str, JsonValue] | None = None,
    remove_sandbox: Iterable[str] = (),
) -> dict[str, JsonValue]:
    """Apply one session policy patch while preserving unrelated rules."""
    path = paths.session(session_id).config_file
    rows = _read_rows(path)
    permission_config = dict(_plugin_config(rows, "permissions"))
    for tool in (*remove_permissions, *(permissions or {})):
        _remove_rule(permission_config, {"tool": re.escape(tool)})
    for tool, decision in (permissions or {}).items():
        permission_config.setdefault(decision, []).insert(
            0, {"tool": re.escape(tool)}
        )
    sandbox_config = dict(_plugin_config(rows, "sandbox"))
    for key in remove_sandbox:
        sandbox_config.pop(key, None)
    sandbox_config.update(sandbox or {})
    if permission_config:
        _replace_plugin(rows, "permissions", permission_config)
    else:
        _remove_plugin(rows, "permissions")
    if sandbox_config:
        _replace_plugin(rows, "sandbox", sandbox_config)
    else:
        _remove_plugin(rows, "sandbox")

    document: dict[str, object] = {"plugins": rows} if rows else {}
    if document:
        _write_yaml(path, document)
    elif path.exists():
        path.unlink()
    return load_session_policy(paths, session_id)


def _remove_rule(permissions: dict[str, JsonValue], rule: dict[str, JsonValue]) -> None:
    for key in _PERMISSION_DECISIONS:
        permissions[key] = [item for item in permissions.get(key, []) if item != rule]
        if not permissions[key]:
            permissions.pop(key, None)


def _read_rows(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return []
    if isinstance(data, list):
        document: object = data
    elif isinstance(data, dict) and ("plugins" in data or "entries" in data):
        document = data
    else:
        raise ValueError(f"{path} must contain a plugin overlay")
    try:
        PluginOverlay.parse(document)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid session plugin overlay: {path}: {exc}") from exc
    values = document if isinstance(document, list) else document.get("plugins", document.get("entries"))
    if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
        raise ValueError(f"{path} must contain plugin entries")
    return [dict(item) for item in values]


def _plugin_config(rows: list[dict[str, object]], plugin_id: str) -> dict[str, JsonValue]:
    for row in rows:
        if row.get("id", row.get("name")) == plugin_id:
            config = row.get("config", {})
            return dict(config) if isinstance(config, dict) else {}
    return {}


def _replace_plugin(
    rows: list[dict[str, object]],
    plugin_id: str,
    config: dict[str, JsonValue],
) -> None:
    for row in rows:
        if row.get("id", row.get("name")) == plugin_id:
            row["config"] = config
            return
    rows.append({"id": plugin_id, "config": config})


def _remove_plugin(rows: list[dict[str, object]], plugin_id: str) -> None:
    rows[:] = [row for row in rows if row.get("id", row.get("name")) != plugin_id]


def _write_yaml(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
