"""Agent definition loading from validated Markdown frontmatter."""

from __future__ import annotations

from pathlib import Path
from collections import OrderedDict
from sys import getsizeof
from threading import RLock
import yaml
from pydantic import JsonValue, TypeAdapter
from XBotv2.agents.api.contracts import AgentDefinition, AgentModelPolicy, AgentToolPolicy
from XBotv2.core.api.domain import AgentExecutionLimits
from XBotv2.core.api.variables import RuntimeVariables
from XBotv2.permissions.api.models import PermissionPolicy

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
    *,
    sources: AgentSources | None = None,
) -> list[AgentDefinition]:
    paths = sorted(directory.glob("*.md")) if directory.is_dir() else []
    if sources is not None:
        sources.retain_directory(directory, paths)
    return [load_definition(path, variables, sources=sources) for path in paths]


def load_definition(
    path: Path,
    variables: RuntimeVariables | None = None,
    *,
    sources: AgentSources | None = None,
) -> AgentDefinition:
    variables = variables or RuntimeVariables()
    raw_metadata, prompt_text = sources.parse(path) if sources else _parse_source(path)
    expanded_metadata = variables.expand_config(
        raw_metadata,
        source=f"Agent frontmatter in {path}",
    )
    try:
        metadata = TypeAdapter(dict[str, JsonValue]).validate_python(
            expanded_metadata
        )
    except ValueError as exc:
        raise ValueError(f"Agent frontmatter must contain JSON values: {path}") from exc
    unknown = set(metadata) - _FIELDS
    if unknown:
        raise ValueError(
            f"Unknown Agent fields in {path}: {', '.join(sorted(unknown))}"
        )
    prompt = variables.expand_markdown(
        prompt_text,
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


class AgentSources:
    """Process-owned, byte-budgeted cache of unexpanded source data."""

    def __init__(self, max_bytes: int = 4 * 1024 * 1024) -> None:
        if max_bytes < 0:
            raise ValueError("Agent source cache budget cannot be negative")
        self._max_bytes = max_bytes
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = RLock()

    def _discard(self, path: Path) -> None:
        entry = self._entries.pop(path, None)
        if entry is not None:
            self._bytes -= entry[2]

    def parse(self, path: Path):
        with self._lock:
            try:
                stat = path.stat()
            except OSError:
                self._discard(path)
                raise
            version = (stat.st_mtime_ns, stat.st_size)
            entry = self._entries.get(path)
            if entry is not None and entry[0] == version:
                self._entries.move_to_end(path)
                return entry[1]
            self._discard(path)
            parsed = _parse_source(path)
            charge = (64 * stat.st_size + getsizeof(path) + getsizeof(str(path))
                      + getsizeof(parsed) + sum(map(getsizeof, parsed)))
            if charge <= self._max_bytes:
                while self._bytes + charge > self._max_bytes:
                    self._discard(next(iter(self._entries)))
                self._entries[path] = (version, parsed, charge)
                self._bytes += charge
            return parsed

    def retain_directory(self, directory: Path, paths: list[Path]) -> None:
        with self._lock:
            present = set(paths)
            for path in tuple(self._entries):
                if path.parent == directory and path not in present:
                    self._discard(path)

    def close(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0


def _parse_source(path: Path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith(f"{_FRONTMATTER}\n"):
        raise ValueError(f"Agent definition requires YAML frontmatter: {path}")
    marker = text.find(f"\n{_FRONTMATTER}\n", len(_FRONTMATTER) + 1)
    if marker < 0:
        raise ValueError(f"Agent definition has unclosed frontmatter: {path}")
    raw_metadata = yaml.safe_load(text[len(_FRONTMATTER) + 1:marker]) or {}
    if not isinstance(raw_metadata, dict):
        raise ValueError(f"Agent frontmatter must be a mapping: {path}")
    return raw_metadata, text[marker + len(_FRONTMATTER) + 2:].strip()
