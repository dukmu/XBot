"""Agent definition loading from Markdown frontmatter.

Parses the agent file format only: frontmatter fields, tool selectors, model
overrides, and prompt expansion.  Permission policy values are carried as raw
data — the permissions plugin owns their validation and normalization.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from XBotv2.agents.contracts import AgentDefinition
from XBotv2.core import RuntimeVariables

_FRONTMATTER = "---"
_FIELDS = {
    "description",
    "mode",
    "provider",
    "model",
    "temperature",
    "max_output_tokens",
    "context_window",
    "max_iterations",
    "steps",
    "permission",
    "permissions",
    "tools",
    "hidden",
}


def load_definitions(
    directory: Path,
    variables: RuntimeVariables | None = None,
) -> list[AgentDefinition]:
    """Load every ``*.md`` definition in *directory* (empty when absent)."""
    if not directory.is_dir():
        return []
    return [
        load_definition(path, variables)
        for path in sorted(directory.glob("*.md"))
    ]


def load_definition(
    path: Path,
    variables: RuntimeVariables | None = None,
) -> AgentDefinition:
    variables = variables or RuntimeVariables()
    text = path.read_text(encoding="utf-8")
    if not text.startswith(f"{_FRONTMATTER}\n"):
        raise ValueError(f"Agent definition requires YAML frontmatter: {path}")
    marker = text.find(f"\n{_FRONTMATTER}\n", len(_FRONTMATTER) + 1)
    if marker < 0:
        raise ValueError(f"Agent definition has unclosed frontmatter: {path}")
    metadata = yaml.safe_load(text[len(_FRONTMATTER) + 1:marker]) or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"Agent frontmatter must be a mapping: {path}")
    unknown = set(metadata) - _FIELDS
    if unknown:
        raise ValueError(
            f"Unknown Agent fields in {path}: {', '.join(sorted(unknown))}"
        )
    prompt = variables.expand_markdown(
        text[marker + len(_FRONTMATTER) + 2:].strip(),
        source=str(path),
    )
    tools, disabled_tools = parse_tools(metadata.get("tools"), path)
    if "permission" in metadata and "permissions" in metadata:
        raise ValueError(f"Use either permission or permissions, not both: {path}")
    permissions = metadata.get("permission", metadata.get("permissions"))
    if permissions is None:
        permissions = {}
    provider, model = parse_model(metadata, path)
    return AgentDefinition(
        name=path.stem,
        description=str(metadata.get("description") or ""),
        mode=str(metadata.get("mode") or "all"),
        prompt=prompt,
        provider=provider,
        model=model,
        temperature=_optional_float(metadata, "temperature"),
        max_output_tokens=_optional_int(metadata, "max_output_tokens"),
        context_window=_optional_int(metadata, "context_window"),
        max_iterations=_optional_int(
            metadata, "max_iterations", alias="steps"
        ),
        permissions=permissions,
        tools=tools,
        disabled_tools=disabled_tools,
        hidden=bool(metadata.get("hidden", False)),
    )


def parse_tools(
    value: Any,
    path: Path,
) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
    """Parse the ``tools`` selector into visible/disabled tool names.

    A list restricts the visible tool set; an OpenCode-style boolean mapping
    only disables the ``false`` entries.  Tool visibility is not a permission
    policy: allowed/denied decisions come from the ``permission`` field.
    """
    if value is None:
        return None, ()
    if isinstance(value, list):
        return tuple(str(tool) for tool in value), ()
    if isinstance(value, dict) and all(
        isinstance(enabled, bool) for enabled in value.values()
    ):
        disabled = tuple(
            str(tool) for tool, visible in value.items() if not visible
        )
        return None, disabled
    raise ValueError(f"Agent tools must be a list or boolean mapping: {path}")


def parse_model(
    metadata: dict[str, Any],
    path: Path,
) -> tuple[str | None, str | None]:
    provider = str(metadata["provider"]) if metadata.get("provider") else None
    model = str(metadata["model"]) if metadata.get("model") else None
    if model is None or "/" not in model:
        return provider, model
    model_provider, model_name = model.split("/", 1)
    if provider is not None and provider != model_provider:
        raise ValueError(
            f"Agent provider {provider!r} conflicts with model {model!r}: {path}"
        )
    return provider or model_provider, model_name


def _optional_float(metadata: dict[str, Any], name: str) -> float | None:
    value = metadata.get(name)
    return float(value) if value is not None else None


def _optional_int(
    metadata: dict[str, Any],
    name: str,
    *,
    alias: str | None = None,
) -> int | None:
    if alias and name in metadata and alias in metadata:
        raise ValueError(f"Use either {name} or {alias}, not both")
    value = metadata.get(name, metadata.get(alias) if alias else None)
    return int(value) if value is not None else None


__all__ = ["load_definition", "load_definitions", "parse_model", "parse_tools"]
