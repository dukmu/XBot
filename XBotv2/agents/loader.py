"""Agent definition loading from validated Markdown frontmatter."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import JsonValue, TypeAdapter

from XBotv2.agents.contracts import (
    AgentDefinition,
    AgentModelPolicy,
    AgentToolPolicy,
)
from XBotv2.core.domain import AgentExecutionLimits
from XBotv2.core.variables import RuntimeVariables
from XBotv2.permissions.contracts import PermissionPolicy

_FRONTMATTER = "---"
_FIELDS = {
    "description",
    "mode",
    "model_policy",
    "limits",
    "permission_policy",
    "tool_policy",
    "hidden",
}


def load_definitions(
    directory: Path,
    variables: RuntimeVariables | None = None,
) -> list[AgentDefinition]:
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
    raw_metadata = yaml.safe_load(text[len(_FRONTMATTER) + 1:marker]) or {}
    if not isinstance(raw_metadata, dict):
        raise ValueError(f"Agent frontmatter must be a mapping: {path}")
    try:
        metadata = TypeAdapter(dict[str, JsonValue]).validate_python(raw_metadata)
    except ValueError as exc:
        raise ValueError(f"Agent frontmatter must contain JSON values: {path}") from exc
    unknown = set(metadata) - _FIELDS
    if unknown:
        raise ValueError(
            f"Unknown Agent fields in {path}: {', '.join(sorted(unknown))}"
        )
    prompt = variables.expand_markdown(
        text[marker + len(_FRONTMATTER) + 2:].strip(),
        source=str(path),
    )
    return AgentDefinition(
        name=path.stem,
        description=str(metadata.get("description") or ""),
        mode=str(metadata.get("mode") or "all"),
        prompt=prompt,
        model_policy=AgentModelPolicy.model_validate(
            metadata.get("model_policy") or {}
        ),
        limits=AgentExecutionLimits.model_validate(metadata.get("limits") or {}),
        permission_policy=PermissionPolicy.model_validate(
            metadata.get("permission_policy") or {"default_decision": "allow"}
        ),
        tool_policy=AgentToolPolicy.model_validate(
            metadata.get("tool_policy") or {}
        ),
        hidden=bool(metadata.get("hidden", False)),
    )


__all__ = ["load_definition", "load_definitions"]
