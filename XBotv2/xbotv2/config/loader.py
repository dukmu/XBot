"""Configuration loader — reads YAML files from disk."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from xbotv2.config.models import ProviderConfig, SystemConfig, UserContext


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


def load_user_context(config_dir: Path) -> UserContext:
    """Load user context from <config_dir>/config/user.yaml."""
    data = load_yaml(config_dir / "config" / "user.yaml")
    return UserContext(**data)


def load_provider_config(config_dir: Path, provider_name: str = "default") -> ProviderConfig:
    """Load provider config from <config_dir>/config/providers.yaml.

    The providers.yaml file can either use the Stage 2 shape
    ``{default: name, providers: {name: config}}`` or directly map provider
    names to config sections. No personality fallback is supported.

    Environment variables like ${DEEPSEEK_API_KEY} are expanded at load time.
    """
    all_data = load_yaml(config_dir / "config" / "providers.yaml")
    if not all_data:
        return ProviderConfig()

    providers = all_data.get("providers") if isinstance(all_data.get("providers"), dict) else all_data
    selected_name = provider_name
    if selected_name == "default" and isinstance(all_data.get("default"), str):
        selected_name = str(all_data["default"])
    section = providers.get(selected_name) if isinstance(providers, dict) else None
    if not section:
        return ProviderConfig()

    section = _expand_env_in_dict(section)
    api_key_env = section.pop("api_key_env", None)
    if api_key_env and not section.get("api_key"):
        section["api_key"] = os.environ.get(str(api_key_env), "")
    return ProviderConfig(**section)


def load_provider_names(config_dir: Path) -> tuple[str, list[str]]:
    """Return the configured default provider name and provider names."""
    all_data = load_yaml(config_dir / "config" / "providers.yaml")
    if not all_data:
        return "default", []
    providers = all_data.get("providers") if isinstance(all_data.get("providers"), dict) else all_data
    names = sorted(str(name) for name in providers if isinstance(providers, dict))
    default = str(all_data.get("default") or "default")
    return default, names


def load_system_config(config_dir: Path, workspace_root: Path | str) -> SystemConfig:
    """Load runtime config from config/system.yaml and workspace AGENTS.md."""
    data = load_yaml(config_dir / "config" / "system.yaml")
    permissions = load_yaml(config_dir / "config" / "permissions.yaml")
    sandbox = load_yaml(config_dir / "config" / "sandbox.yaml")
    if permissions:
        data["permissions"] = permissions
    if sandbox:
        data["sandbox"] = sandbox
    workspace = Path(workspace_root)
    agents_path = workspace / "AGENTS.md"
    if agents_path.exists():
        agents_text = agents_path.read_text(encoding="utf-8")
        existing = str(data.get("instructions") or "")
        data["instructions"] = "\n\n".join(
            part for part in (existing, agents_text) if part.strip()
        )
    return SystemConfig(**data)


