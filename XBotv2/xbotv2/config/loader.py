"""Configuration loader — reads YAML files from disk."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from xbotv2.config.models import AgentConfig, ProviderConfig, UserContext

DEFAULT_SYSTEM_TEMPLATE = """\
You are {agent_name}, {agent_role}.

User: {user_name} ({user_id})
Platform: {platform}
"""


def _expand_env(value: str) -> str:
    """Replace ${VAR} or $VAR patterns with environment variable values."""
    if not isinstance(value, str):
        return value
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

    def replacer(match):
        var_name = match.group(1)
        return os.environ.get(var_name, "")

    return pattern.sub(replacer, value)


def _expand_env_in_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively expand env vars in all string values of a dict."""
    result = {}
    for key, value in data.items():
        if isinstance(value, str):
            result[key] = _expand_env(value)
        elif isinstance(value, dict):
            result[key] = _expand_env_in_dict(value)
        elif isinstance(value, list):
            result[key] = [
                _expand_env_in_dict(item) if isinstance(item, dict)
                else _expand_env(item) if isinstance(item, str)
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
    """Load provider config from <config_dir>/config/provider.yaml.

    The provider.yaml file can contain multiple provider sections keyed
    by name (e.g. 'deepseek', 'lmstudio', 'openai'). The *provider_name*
    selects which section to use; falls back to the 'default' key.

    Environment variables like ${DEEPSEEK_API_KEY} are expanded at load time.
    """
    all_data = load_yaml(config_dir / "config" / "provider.yaml")
    if not all_data:
        return ProviderConfig()

    # Select the named provider section, falling back to 'default'
    section = all_data.get(provider_name) or all_data.get("default", {})
    if not section:
        return ProviderConfig()

    # Expand env vars
    section = _expand_env_in_dict(section)
    return ProviderConfig(**section)


def load_agent_config(config_dir: Path, personality_id: str = "default") -> AgentConfig:
    """Load personality config from data/personalities/<id>/personality.yaml."""
    personality_dir = config_dir / "personalities" / personality_id
    data = load_yaml(personality_dir / "personality.yaml")

    # Load text files
    instructions_path = personality_dir / "instructions.md"
    if instructions_path.exists():
        data.setdefault("instructions", instructions_path.read_text())

    memory_path = personality_dir / "memory.md"
    if memory_path.exists():
        data.setdefault("memory", memory_path.read_text())

    return AgentConfig(**data)


def load_user_context_simple(config_dir: Path) -> dict[str, Any]:
    """Load user context as a plain dict (non-Pydantic path)."""
    return load_yaml(config_dir / "user.yaml")
