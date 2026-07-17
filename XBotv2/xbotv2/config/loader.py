"""Configuration loader — reads YAML files from disk."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from xbotv2.config.models import (
    HookConfig,
    ProviderConfig,
    SystemConfig,
    UserContext,
    WorkspacePluginConfig,
)
from xbotv2.config.policy import merge_permission_config, merge_sandbox_config
from xbotv2.api.paths import RuntimePaths


def expand_env(value: str) -> str:
    """Replace ${VAR} or $VAR patterns with environment variable values."""
    if not isinstance(value, str):
        return value
    pattern = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
    return pattern.sub(lambda m: os.environ.get(m.group(1), ""), value)


def _expand_env_in_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively expand env vars in all string values of a dict."""
    result = {}
    for key, value in data.items():
        if isinstance(value, str):
            result[key] = expand_env(value)
        elif isinstance(value, dict):
            result[key] = _expand_env_in_dict(value)
        elif isinstance(value, list):
            result[key] = [
                _expand_env_in_dict(item) if isinstance(item, dict)
                else expand_env(item) if isinstance(item, str)
                else item
                for item in value
            ]
        else:
            result[key] = value
    return result


def load_yaml(path: Path) -> dict[str, Any]:
    """Read and parse a YAML file. Returns {} if the file is missing."""
    if not path.exists():
        return {}
    with open(path) as f:
        result = yaml.safe_load(f)
    return result if result is not None else {}


def load_user_context(paths: RuntimePaths) -> UserContext:
    """Load the global user context."""
    data = load_yaml(paths.user_config)
    return UserContext(**data)


def load_provider_config(paths: RuntimePaths, provider_name: str = "default") -> ProviderConfig:
    """Load one provider configuration.

    The providers.yaml file can either use the Stage 2 shape
    ``{default: name, providers: {name: config}}`` or directly map provider
    names to config sections. No personality fallback is supported.

    Environment variables like ${DEEPSEEK_API_KEY} are expanded at load time.
    """
    all_data = load_yaml(paths.providers_config)
    if not all_data:
        if provider_name != "default":
            raise ValueError(
                f"Unknown provider config: {provider_name}. No providers are configured."
            )
        return ProviderConfig()

    providers = all_data.get("providers") if isinstance(all_data.get("providers"), dict) else all_data
    selected_name = provider_name
    if selected_name == "default" and isinstance(all_data.get("default"), str):
        selected_name = str(all_data["default"])
    section = providers.get(selected_name) if isinstance(providers, dict) else None
    if section is None:
        available = ", ".join(sorted(str(name) for name in providers))
        raise ValueError(
            f"Unknown provider config: {provider_name}. Available providers: {available or '(none)'}."
        )

    section = _expand_env_in_dict(section)
    api_key_env = section.pop("api_key_env", None)
    if api_key_env and not section.get("api_key"):
        section["api_key"] = os.environ.get(str(api_key_env), "")
    return ProviderConfig(**section)


def load_provider_names(paths: RuntimePaths) -> tuple[str, list[str]]:
    """Return the configured default provider name and provider names."""
    all_data = load_yaml(paths.providers_config)
    if not all_data:
        return "default", []
    nested = isinstance(all_data.get("providers"), dict)
    providers = all_data["providers"] if nested else all_data
    names = sorted(str(name) for name in providers if isinstance(providers, dict))
    default = str(all_data.get("default") or "default") if nested else (
        "default" if "default" in names else names[0] if names else "default"
    )
    return default, names


def load_system_config(paths: RuntimePaths, workspace_root: Path | str) -> SystemConfig:
    """Load global configuration and startup-only workspace overlays."""
    data = load_yaml(paths.system_config)
    permissions = load_yaml(paths.permissions_config)
    sandbox = load_yaml(paths.sandbox_config)
    if permissions:
        data["permissions"] = permissions
    if sandbox:
        data["sandbox"] = sandbox
    workspace = Path(workspace_root).resolve()
    _apply_workspace_overlays(data, workspace)
    memory_path = paths.memory_file
    if memory_path.exists():
        data["memory"] = memory_path.read_text(encoding="utf-8")
    return SystemConfig(**data)


def _apply_workspace_overlays(data: dict[str, Any], workspace: Path) -> None:
    config_dir = workspace / ".xbot"

    policy_path = config_dir / "policy.yaml"
    policy = load_yaml(policy_path)
    if policy_path.exists():
        _require_mapping(policy, ".xbot/policy.yaml")
    if policy:
        unknown = set(policy) - {"permissions", "sandbox"}
        if unknown:
            raise ValueError(
                f"Unknown .xbot/policy.yaml keys: {', '.join(sorted(unknown))}"
            )
        workspace_permissions = policy.get("permissions")
        workspace_sandbox = policy.get("sandbox")
        _require_optional_mapping(
            workspace_permissions, ".xbot/policy.yaml permissions"
        )
        _require_optional_mapping(workspace_sandbox, ".xbot/policy.yaml sandbox")
        data["permissions"] = merge_permission_config(
            data.get("permissions"), workspace_permissions
        )
        data["sandbox"] = merge_sandbox_config(
            data.get("sandbox"), workspace_sandbox
        )

    plugins_path = config_dir / "plugins.yaml"
    plugins_doc = load_yaml(plugins_path)
    if plugins_path.exists():
        _require_mapping(plugins_doc, ".xbot/plugins.yaml")
    if plugins_doc:
        unknown = set(plugins_doc) - {"paths", "plugins"}
        if unknown:
            raise ValueError(
                f"Unknown .xbot/plugins.yaml keys: {', '.join(sorted(unknown))}"
            )
        plugin_paths = plugins_doc.get("paths") or []
        if not isinstance(plugin_paths, list):
            raise ValueError(".xbot/plugins.yaml paths must be a list")
        data["plugin_paths"] = [
            str(_workspace_path(workspace, value)) for value in plugin_paths
        ]
        configured = dict(data.get("plugins") or {})
        disabled: list[str] = []
        entries = plugins_doc.get("plugins") or {}
        if not isinstance(entries, dict):
            raise ValueError(".xbot/plugins.yaml plugins must be a mapping")
        for name, raw in entries.items():
            entry = WorkspacePluginConfig.model_validate(
                {} if raw is None else raw
            )
            if entry.enabled:
                configured[str(name)] = entry.config
            else:
                configured.pop(str(name), None)
                disabled.append(str(name))
        data["plugins"] = configured
        data["disabled_plugins"] = disabled

    hooks_path = config_dir / "hooks.yaml"
    if hooks_path.exists():
        hooks_doc = load_yaml(hooks_path)
        _require_mapping(hooks_doc, ".xbot/hooks.yaml")
        if set(hooks_doc) - {"hooks"}:
            raise ValueError(".xbot/hooks.yaml only supports the hooks key")
        hooks = hooks_doc.get("hooks") or []
        if not isinstance(hooks, list):
            raise ValueError(".xbot/hooks.yaml hooks must be a list")
        declarations = [
            HookConfig.model_validate(item).model_copy(
                update={"base_dir": config_dir}
            )
            for item in hooks
        ]
        data["hooks"] = declarations


def _workspace_path(workspace: Path, value: Any) -> Path:
    path = (workspace / str(value)).resolve()
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("Workspace plugin paths must stay inside the workspace") from exc
    if not path.is_dir():
        raise ValueError(f"Workspace plugin path is not a directory: {path}")
    return path


def _require_mapping(value: Any, source: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{source} must contain a mapping")


def _require_optional_mapping(value: Any, source: str) -> None:
    if value is not None:
        _require_mapping(value, source)
