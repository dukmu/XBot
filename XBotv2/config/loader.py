"""Load and resolve validated configuration layers."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from XBotv2.core.paths import RuntimePaths
from XBotv2.config.models import (
    ConfigOverlay,
    ProviderConfig,
    RuntimeConfig,
)
from XBotv2.config.policy import merge_permission_config, merge_sandbox_config


_ENV = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def expand_env(value: str) -> str:
    """Expand environment references and reject missing variables."""
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"Environment variable {name} is not set")
        return os.environ[name]

    return _ENV.sub(replace, value)


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return expand_env(value)
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    return value


def load_yaml(path: Path) -> dict[str, Any]:
    """Read one UTF-8 YAML mapping; a missing file is an empty layer."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def parse_provider_config(
    name: str,
    raw: dict[str, Any],
    *,
    require_key: bool = True,
) -> ProviderConfig:
    """Validate one provider definition (env expansion + api_key_env).

    With ``require_key=True`` (a provider selection) an ``api_key_env``
    variable must be set; with ``require_key=False`` (e.g. the ``/providers``
    listing) the key is left unresolved so listing never fails on unrelated
    providers.
    """
    values = _expand_env(dict(raw))
    api_key_env = values.pop("api_key_env", None)
    if api_key_env and not values.get("api_key"):
        env_name = str(api_key_env)
        if require_key and env_name not in os.environ:
            raise ValueError(f"Environment variable {env_name} is not set")
        if env_name in os.environ:
            values["api_key"] = os.environ[env_name]
    return ProviderConfig.model_validate(values)


def resolve_llm_config(paths: RuntimePaths | Path) -> dict[str, Any]:
    """Resolve the merged ``llm`` plugin entry config from the plugin tree.

    Reads the bundled ``xcore.yaml`` merged with the global user tree
    (``<data_dir>/config/plugins.yaml``) and returns the ``llm`` entry's
    ``config`` block (``default`` + ``providers``).  Used by CLI startup
    validation and server-root provider listing, which run before a session
    mounts the llm plugin; the mounted plugin uses its own tree config.
    """
    from XBotv2.bootstrap import DEFAULT_TREE
    from XBotv2.loader import PluginTree

    if not isinstance(paths, RuntimePaths):
        paths = RuntimePaths.from_data_dir(paths)
    tree = PluginTree.from_yaml(DEFAULT_TREE)
    plugins_file = paths.config_dir / "plugins.yaml"
    if plugins_file.exists():
        tree = tree.merged_with(PluginTree.from_yaml(plugins_file))
    entry = next((item for item in tree.entries if item.id == "llm"), None)
    return dict(entry.config or {}) if entry else {}


def load_runtime_config(
    paths: RuntimePaths,
    workspace_root: Path | str,
    session_id: str | None = None,
) -> RuntimeConfig:
    """Resolve defaults, global, session, and workspace configuration."""
    workspace = Path(workspace_root).resolve()
    workspace_config = workspace / ".xbot" / "config.yaml"
    layers = [
        _load_overlay(paths.config_file),
        _load_overlay(
            paths.session(session_id).config_file if session_id else None
        ),
        _load_overlay(workspace_config, workspace=workspace),
    ]
    merged: dict[str, Any] = {}
    for layer in layers:
        values = layer.model_dump(exclude_unset=True, exclude_none=True)
        permissions = values.pop("permissions", None)
        sandbox = values.pop("sandbox", None)
        merged = _merge(merged, values)
        if permissions is not None:
            merged["permissions"] = merge_permission_config(
                merged.get("permissions"), permissions
            )
        if sandbox is not None:
            merged["sandbox"] = merge_sandbox_config(
                merged.get("sandbox"), sandbox
            )
    config = RuntimeConfig.model_validate(merged)
    if layers[-1].hooks is not None:
        config.hooks = [
            hook.model_copy(update={"base_dir": workspace_config.parent})
            for hook in config.hooks
        ]
    if layers[-1].workspace_tools is not None:
        config.workspace_tools = [
            tool.model_copy(update={"base_dir": workspace_config.parent})
            for tool in config.workspace_tools
        ]
    if paths.memory_file.exists():
        config.memory = paths.memory_file.read_text(encoding="utf-8")
    return config


def _load_overlay(
    path: Path | None,
    *,
    workspace: Path | None = None,
) -> ConfigOverlay:
    if path is None:
        return ConfigOverlay()
    overlay = ConfigOverlay.model_validate(load_yaml(path))
    updates: dict[str, Any] = {}
    if workspace is not None and overlay.plugin_paths is not None:
        updates["plugin_paths"] = [
            str(_workspace_path(workspace, value))
            for value in overlay.plugin_paths
        ]
    return overlay.model_copy(update=updates) if updates else overlay


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge(current, value)
        else:
            merged[key] = value
    return merged


def _workspace_path(workspace: Path, value: Any) -> Path:
    path = (workspace / str(value)).resolve()
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("Workspace plugin paths must stay inside the workspace") from exc
    if not path.is_dir():
        raise ValueError(f"Workspace plugin path is not a directory: {path}")
    return path


__all__ = [
    "expand_env",
    "load_runtime_config",
    "load_yaml",
    "parse_provider_config",
    "resolve_llm_config",
]
