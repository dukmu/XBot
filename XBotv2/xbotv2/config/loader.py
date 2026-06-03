"""Configuration loader — reads YAML files from disk."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from xbotv2.config.models import AgentConfig, ProviderConfig, UserContext

DEFAULT_SYSTEM_TEMPLATE = """\
You are {agent_name}, {agent_role}.

User: {user_name} ({user_id})
Platform: {platform}
"""


def load_yaml(path: Path) -> dict[str, Any]:
    """Read and parse a YAML file. Returns {} if the file is missing."""
    if not path.exists():
        return {}
    with open(path) as f:
        result = yaml.safe_load(f)
    return result if result is not None else {}


def load_user_context(config_dir: Path) -> UserContext:
    """Load user context from data/config/user.yaml."""
    data = load_yaml(config_dir / "user.yaml")
    return UserContext(**data)


def load_provider_config(config_dir: Path, _provider_name: str = "default") -> ProviderConfig:
    """Load provider config from data/config/provider.yaml.

    Args:
        config_dir: Root data directory.
        _provider_name: Reserved for multi-provider support (currently unused).
    """
    data = load_yaml(config_dir / "provider.yaml")
    return ProviderConfig(**data)


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

    # Load permissions/sandbox from separate files
    permissions_path = personality_dir / "permissions.json"
    if permissions_path.exists():
        import json
        data.setdefault("permissions", json.loads(permissions_path.read_text()))

    sandbox_path = personality_dir / "sandbox.json"
    if sandbox_path.exists():
        import json
        data.setdefault("sandbox", json.loads(sandbox_path.read_text()))

    return AgentConfig(**data)


def load_user_context_simple(config_dir: Path) -> dict[str, Any]:
    """Load user context as a plain dict (non-Pydantic path)."""
    return load_yaml(config_dir / "user.yaml")
