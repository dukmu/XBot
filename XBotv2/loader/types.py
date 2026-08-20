"""Public plugin-tree declarations and configuration parsing.

Reference model: DeepSeek Harness's Cordis loader
(``@cordisjs/plugin-loader`` + ``@cordisjs/plugin-include``).  The runtime is
declared as a tree of plugin *entries* (id / module name / config / disabled /
inject / isolate); a loader service imports each module, resolves the plugin
it exports, and mounts it on the XCore context (optionally on an isolated
scope).  Dependencies are expressed with XCore's ``inject`` (services) and the
tree's load order -- there is no separate plugin system beside XCore's.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

class LoadError(RuntimeError):
    """A tree entry failed to load or did not activate."""


@dataclass
class PluginEntry:
    """One configured plugin node in the tree (aligned with EntryOptions)."""

    id: str
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    disabled: bool = False
    # False marks session-lifecycle entries whose live re-apply would destroy
    # engine/session state; the soft-restart path skips them.
    reloadable: bool = True
    isolate: dict[str, Any] | None = None
    # Bundled entries may opt into one or more application profiles. External
    # entries without a profile belong to the Agent profile by default.
    profiles: frozenset[str] | None = None


_MISSING = object()


def _lookup(values: dict[str, Any] | None, ref: str) -> Any:
    """Resolve ``a.b.c`` against a values mapping (set memberships allowed).

    Returns :data:`_MISSING` when a key does not exist (so an existing key
    with a ``None`` value stays resolvable).
    """
    if values is None:
        return _MISSING
    parts = ref.split(".")
    target: Any = values
    for index, part in enumerate(parts):
        if isinstance(target, dict):
            if part not in target:
                return _MISSING
            target = target[part]
        elif isinstance(target, (set, frozenset)):
            if index != len(parts) - 1:
                return _MISSING
            return part in target
        elif isinstance(target, (list, tuple)) and part.isdigit():
            position = int(part)
            if position >= len(target):
                return _MISSING
            target = target[position]
        else:
            return _MISSING
    return target


def _resolve_ref(value: Any, values: dict[str, Any] | None) -> Any:
    """Resolve ``${name}`` / ``${env:VAR}`` references in tree config.

    Mirrors DeepSeek Harness's ``!!js`` expressions in cordis.patch.yml: the
    tree is fully declarative and the composition root supplies the dynamic
    session values (paths, state store, provider, child-engine factory, ...)
    as a plain mapping.  ``${env:NAME}`` reads the process environment.
    """
    if values is None:
        return value
    if isinstance(value, str):
        match = re.fullmatch(r"\$\{([^}]+)\}", value)
        if match:
            ref = match.group(1)
            if ref.startswith("env:"):
                return os.environ.get(ref[4:], "")
            resolved = _lookup(values, ref)
            if resolved is _MISSING:
                # Unknown references stay literal: runtime variables such as
                # ${workspace} are expanded by the consuming service.
                return value
            return resolved
        return value
    if isinstance(value, dict):
        return {key: _resolve_ref(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_ref(item, values) for item in value]
    return value


def _entry_from_dict(
    data: dict[str, Any], values: dict[str, Any] | None = None
) -> PluginEntry:
    resolved_config = _resolve_ref(data.get("config") or {}, values)
    if not isinstance(resolved_config, dict):
        # A bare ${...} reference without runtime values stays literal;
        # treat it as the default (no config).
        resolved_config = {}
    raw_profiles = data.get("profiles")
    if raw_profiles is None:
        profiles = None
    elif isinstance(raw_profiles, str):
        profiles = frozenset({raw_profiles})
    elif isinstance(raw_profiles, list) and all(
        isinstance(item, str) and item for item in raw_profiles
    ):
        profiles = frozenset(raw_profiles)
    else:
        raise TypeError("plugin entry profiles must be a string or list of strings")
    entry = PluginEntry(
        id=str(data.get("id") or data.get("name")),
        name=str(data["name"]),
        config=dict(resolved_config or {}),
        disabled=bool(_resolve_ref(data.get("disabled", False), values)),
        reloadable=bool(_resolve_ref(data.get("reloadable", True), values)),
        isolate=_resolve_ref(data.get("isolate"), values),
        profiles=profiles,
    )
    if not entry.id:
        raise ValueError("plugin tree entry requires an id or name")
    return entry


class PluginTree:
    """Ordered list of plugin entries (from a dict, YAML file, or entries)."""

    def __init__(self, entries: list[PluginEntry]) -> None:
        seen: set[str] = set()
        for entry in entries:
            if entry.id in seen:
                raise ValueError(f"duplicate plugin tree entry id: {entry.id}")
            seen.add(entry.id)
        self.entries = list(entries)

    @classmethod
    def from_dict(cls, data: Any) -> "PluginTree":
        if data is None:
            return cls([])
        if isinstance(data, dict):
            raw = data.get("plugins") or data.get("entries") or []
        elif isinstance(data, list):
            raw = data
        else:
            raise TypeError("plugin tree must be a list of entries or {plugins: [...]}")
        if not isinstance(raw, list):
            raise TypeError("plugin tree entries must be a list")
        return cls([_entry_from_dict(item) for item in raw])

    @classmethod
    def from_yaml(
        cls, path: Path | str, values: dict[str, Any] | None = None
    ) -> "PluginTree":
        with open(path, encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
        if isinstance(data, dict):
            raw = data.get("plugins") or data.get("entries") or []
        elif isinstance(data, list):
            raw = data
        else:
            raw = []
        return cls([_entry_from_dict(item, values) for item in raw])

    def merged_with(self, other: "PluginTree") -> "PluginTree":
        """Later tree overrides entries with the same id; others append.

        The later entry's ``config`` is deep-merged into the base entry's
        config (so an overlay can patch one field without restating the
        session-dynamic values); ``disabled`` / ``reloadable`` / ``isolate`` /
        ``profiles`` are replaced by the later entry. Service dependencies are
        plugin declarations and are intentionally absent from tree entries.
        """
        merged: dict[str, PluginEntry] = {entry.id: entry for entry in self.entries}
        for entry in other.entries:
            existing = merged.get(entry.id)
            if existing is not None:
                entry = PluginEntry(
                    id=entry.id,
                    name=entry.name,
                    config=_merge_config(existing.config, entry.config),
                    disabled=entry.disabled,
                    reloadable=entry.reloadable,
                    isolate=entry.isolate if entry.isolate is not None else existing.isolate,
                    profiles=(
                        entry.profiles
                        if entry.profiles is not None
                        else existing.profiles
                    ),
                )
            merged[entry.id] = entry
        return PluginTree(list(merged.values()))

    def excluding(self, entry_ids: set[str]) -> "PluginTree":
        """Return a tree without the given entry ids."""
        return PluginTree(
            [entry for entry in self.entries if entry.id not in entry_ids]
        )

    def for_profile(self, profile: str) -> "PluginTree":
        """Select entries for one application profile.

        Unscoped entries are Agent plugins. Server/client extensions must opt
        in explicitly so a user Agent plugin cannot accidentally execute in a
        process-wide host.
        """
        return PluginTree([
            entry
            for entry in self.entries
            if (
                profile in entry.profiles
                if entry.profiles is not None
                else profile == "agent"
            )
        ])


def _merge_config(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        merged[key] = (
            _merge_config(current, value)
            if isinstance(current, dict) and isinstance(value, dict)
            else value
        )
    return merged


__all__ = ["LoadError", "PluginEntry", "PluginTree"]
