"""Validated plugin-tree declarations and configuration overlays."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class LoadError(RuntimeError):
    """A tree entry failed to load or did not activate."""


@dataclass(frozen=True, slots=True)
class PluginEntry:
    """One complete, resolved plugin declaration."""

    id: str
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    disabled: bool = False
    isolate: dict[str, str | bool] | None = None
    profiles: frozenset[str] | None = None


class _Unset:
    __slots__ = ()


UNSET = _Unset()


@dataclass(frozen=True, slots=True)
class PluginPatch:
    """A partial declaration that preserves omitted fields."""

    id: str
    name: str | _Unset = UNSET
    config: dict[str, Any] | _Unset = UNSET
    disabled: bool | _Unset = UNSET
    isolate: dict[str, str | bool] | None | _Unset = UNSET
    profiles: frozenset[str] | None | _Unset = UNSET


_ENTRY_FIELDS = frozenset({"id", "name", "config", "disabled", "isolate", "profiles"})
_TOP_LEVEL_FIELDS = frozenset({"plugins", "entries"})


def _resolve_ref(value: Any) -> Any:
    if isinstance(value, str):
        match = re.fullmatch(r"\$\{([^}]+)\}", value)
        if match:
            ref = match.group(1)
            if ref.startswith("env:"):
                return os.environ.get(ref[4:], "")
        return value
    if isinstance(value, dict):
        return {key: _resolve_ref(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_ref(item) for item in value]
    return value


def _raw_entries(data: Any) -> list[Any]:
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        raise TypeError("plugin document must be a list or a mapping with 'plugins'")
    unknown = set(data) - _TOP_LEVEL_FIELDS
    if unknown:
        raise ValueError(
            f"unknown plugin document fields: {sorted(map(str, unknown))}"
        )
    present = [key for key in _TOP_LEVEL_FIELDS if key in data]
    if len(present) != 1:
        raise ValueError("plugin document must contain exactly one of 'plugins' or 'entries'")
    raw = data[present[0]]
    if not isinstance(raw, list):
        raise TypeError("plugin document entries must be a list")
    return raw


def _mapping(value: Any, *, subject: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{subject} must be a mapping")
    unknown = set(value) - _ENTRY_FIELDS
    if unknown:
        raise ValueError(f"unknown {subject} fields: {sorted(map(str, unknown))}")
    return value


def _identifier(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise TypeError(f"plugin {field_name} must be a non-empty string")
    return value


def _entry_id(data: dict[str, Any]) -> str:
    value = data.get("id", data.get("name"))
    if value is None:
        raise ValueError("plugin declaration requires an id or name")
    return _identifier(value, field_name="id")


def _config(value: Any) -> dict[str, Any]:
    resolved = _resolve_ref(value)
    if not isinstance(resolved, dict):
        raise TypeError("plugin config must resolve to a mapping")
    return dict(resolved)


def _disabled(value: Any) -> bool:
    resolved = _resolve_ref(value)
    if not isinstance(resolved, bool):
        raise TypeError("plugin disabled must resolve to a boolean")
    return resolved


def _isolate(value: Any) -> dict[str, str | bool] | None:
    resolved = _resolve_ref(value)
    if resolved is None:
        return None
    if not isinstance(resolved, dict):
        raise TypeError("plugin isolate must resolve to a mapping or null")
    result: dict[str, str | bool] = {}
    for service, label in resolved.items():
        if not isinstance(service, str) or not service:
            raise TypeError("plugin isolate service names must be non-empty strings")
        if label is not True and (not isinstance(label, str) or not label):
            raise TypeError("plugin isolate labels must be true or non-empty strings")
        result[service] = label
    return result


def _profiles(value: Any) -> frozenset[str] | None:
    resolved = _resolve_ref(value)
    if resolved is None:
        return None
    if isinstance(resolved, str):
        resolved = [resolved]
    if not isinstance(resolved, list) or not resolved or not all(
        isinstance(item, str) and item for item in resolved
    ):
        raise TypeError("plugin profiles must be a string or non-empty list of strings")
    return frozenset(resolved)


def _entry_from_dict(value: Any) -> PluginEntry:
    data = _mapping(value, subject="entry")
    if "name" not in data:
        raise ValueError("complete plugin entry requires a name")
    return PluginEntry(
        id=_entry_id(data),
        name=_identifier(data["name"], field_name="name"),
        config=_config(data.get("config", {})),
        disabled=_disabled(data.get("disabled", False)),
        isolate=_isolate(data.get("isolate")),
        profiles=_profiles(data.get("profiles")),
    )


def _patch_from_dict(value: Any) -> PluginPatch:
    data = _mapping(value, subject="patch")
    return PluginPatch(
        id=_entry_id(data),
        name=(
            _identifier(data["name"], field_name="name")
            if "name" in data
            else UNSET
        ),
        config=_config(data["config"]) if "config" in data else UNSET,
        disabled=_disabled(data["disabled"]) if "disabled" in data else UNSET,
        isolate=_isolate(data["isolate"]) if "isolate" in data else UNSET,
        profiles=_profiles(data["profiles"]) if "profiles" in data else UNSET,
    )


class PluginOverlay:
    """An ordered, validated collection of partial plugin declarations."""

    def __init__(self, patches: list[PluginPatch]) -> None:
        seen: set[str] = set()
        for patch in patches:
            if patch.id in seen:
                raise ValueError(f"duplicate plugin patch id: {patch.id}")
            seen.add(patch.id)
        self.patches = list(patches)

    @classmethod
    def from_dict(cls, data: Any) -> "PluginOverlay":
        return cls([_patch_from_dict(item) for item in _raw_entries(data)])

    @classmethod
    def from_yaml(cls, path: Path | str) -> "PluginOverlay":
        with open(path, encoding="utf-8") as stream:
            return cls.from_dict(yaml.safe_load(stream))


class PluginTree:
    """An ordered collection of complete plugin declarations."""

    def __init__(self, entries: list[PluginEntry]) -> None:
        seen: set[str] = set()
        for entry in entries:
            if entry.id in seen:
                raise ValueError(f"duplicate plugin tree entry id: {entry.id}")
            seen.add(entry.id)
        self.entries = list(entries)

    @classmethod
    def from_dict(cls, data: Any) -> "PluginTree":
        return cls([_entry_from_dict(item) for item in _raw_entries(data)])

    @classmethod
    def from_yaml(cls, path: Path | str) -> "PluginTree":
        with open(path, encoding="utf-8") as stream:
            return cls.from_dict(yaml.safe_load(stream))

    def patched_with(
        self,
        overlay: PluginOverlay,
        *,
        excluded: frozenset[str] = frozenset(),
        allow_new: bool = True,
    ) -> "PluginTree":
        merged = {entry.id: entry for entry in self.entries}
        for patch in overlay.patches:
            if patch.id in excluded:
                continue
            current = merged.get(patch.id)
            if current is None:
                if not allow_new:
                    if isinstance(patch.name, _Unset):
                        raise ValueError(f"unknown plugin patch id: {patch.id!r}")
                    continue
                if isinstance(patch.name, _Unset):
                    raise ValueError(f"new plugin patch {patch.id!r} requires a name")
                current = PluginEntry(id=patch.id, name=patch.name)
            merged[patch.id] = _merge_entry(current, patch)
        return PluginTree(list(merged.values()))

    def excluding(self, entry_ids: set[str] | frozenset[str]) -> "PluginTree":
        return PluginTree([entry for entry in self.entries if entry.id not in entry_ids])

    def for_profile(self, profile: str) -> "PluginTree":
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


def _merge_entry(current: PluginEntry, patch: PluginPatch) -> PluginEntry:
    return PluginEntry(
        id=patch.id,
        name=current.name if isinstance(patch.name, _Unset) else patch.name,
        config=(
            current.config
            if isinstance(patch.config, _Unset)
            else _merge_config(current.config, patch.config)
        ),
        disabled=(
            current.disabled
            if isinstance(patch.disabled, _Unset)
            else patch.disabled
        ),
        isolate=(
            current.isolate
            if isinstance(patch.isolate, _Unset)
            else patch.isolate
        ),
        profiles=(
            current.profiles
            if isinstance(patch.profiles, _Unset)
            else patch.profiles
        ),
    )


__all__ = [
    "LoadError",
    "PluginEntry",
    "PluginOverlay",
    "PluginPatch",
    "PluginTree",
]
