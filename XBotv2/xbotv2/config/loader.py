"""Load and resolve validated configuration layers."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from xbotv2.api.paths import RuntimePaths
from xbotv2.config.models import (
    ConfigOverlay,
    ProviderConfig,
    RuntimeConfig,
    UserContext,
)
from xbotv2.config.policy import merge_permission_config, merge_sandbox_config


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


def load_user_context(paths: RuntimePaths) -> UserContext:
    return UserContext.model_validate(load_yaml(paths.user_config))


def load_provider_config(
    paths: RuntimePaths,
    provider_name: str = "default",
) -> ProviderConfig:
    """Load one named provider from the canonical providers document."""
    document = load_yaml(paths.providers_config)
    if not document:
        if provider_name != "default":
            raise ValueError(
                f"Unknown provider config: {provider_name}. "
                "No providers are configured."
            )
        return ProviderConfig()
    providers = document.get("providers")
    if not isinstance(providers, dict):
        raise ValueError("providers.yaml requires a providers mapping")
    selected = (
        str(document.get("default") or "")
        if provider_name == "default"
        else provider_name
    )
    section = providers.get(selected)
    if not isinstance(section, dict):
        available = ", ".join(sorted(str(name) for name in providers))
        raise ValueError(
            f"Unknown provider config: {provider_name}. "
            f"Available providers: {available or '(none)'}."
        )
    values = _expand_env(section)
    api_key_env = values.pop("api_key_env", None)
    if api_key_env and not values.get("api_key"):
        name = str(api_key_env)
        if name not in os.environ:
            raise ValueError(f"Environment variable {name} is not set")
        values["api_key"] = os.environ[name]
    return ProviderConfig.model_validate(values)


def load_provider_names(paths: RuntimePaths) -> tuple[str, list[str]]:
    document = load_yaml(paths.providers_config)
    if not document:
        return "default", []
    providers = document.get("providers")
    if not isinstance(providers, dict):
        raise ValueError("providers.yaml requires a providers mapping")
    names = sorted(str(name) for name in providers)
    default = str(document.get("default") or "")
    if default not in providers:
        raise ValueError(f"Unknown default provider: {default or '(empty)'}")
    return default, names


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
    "load_provider_config",
    "load_provider_names",
    "load_runtime_config",
    "load_user_context",
    "load_yaml",
]
