"""Typed thread metadata and its durable mutation boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from XBotv2.core.tools import JsonObject, json_object

THREAD_METADATA_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ThreadMetadata:
    schema_version: int = THREAD_METADATA_SCHEMA_VERSION
    agent: str = ""
    agent_definition: JsonObject | None = None
    provider: str = ""
    model: str = ""
    model_mode: str = ""
    context_window: int = 0
    parent_thread_id: str = ""
    workspace_root: str = ""
    title: str = ""

    @classmethod
    def from_state(cls, value: Mapping[str, object]) -> "ThreadMetadata":
        known = {
            "agent", "agent_definition", "provider", "model", "model_mode",
            "context_window", "parent_thread_id", "workspace_root", "title",
        }
        unknown = set(value) - known
        if unknown:
            raise ValueError(
                "Unknown thread metadata fields: " + ", ".join(sorted(unknown))
            )
        definition = value.get("agent_definition")
        if definition is not None and not isinstance(definition, Mapping):
            raise TypeError("agent_definition must be an object or null")
        return cls(
            agent=_optional_string(value, "agent"),
            agent_definition=(
                json_object(definition) if isinstance(definition, Mapping) else None
            ),
            provider=_optional_string(value, "provider"),
            model=_optional_string(value, "model"),
            model_mode=_optional_string(value, "model_mode"),
            context_window=_optional_integer(value, "context_window"),
            parent_thread_id=_optional_string(value, "parent_thread_id"),
            workspace_root=_optional_string(value, "workspace_root"),
            title=_optional_string(value, "title"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ThreadMetadata":
        expected = {
            "schema_version", "agent", "agent_definition", "provider",
            "model", "model_mode", "context_window", "parent_thread_id",
            "workspace_root", "title",
        }
        if set(value) != expected:
            missing = sorted(expected - set(value))
            unknown = sorted(set(value) - expected)
            raise ValueError(
                "ThreadMetadata fields mismatch; "
                f"missing={missing}, unknown={unknown}"
            )
        version = _integer(value["schema_version"], "schema_version")
        if version != THREAD_METADATA_SCHEMA_VERSION:
            raise ValueError(f"Unsupported ThreadMetadata schema version: {version}")
        return cls.from_state({
            key: value[key] for key in expected - {"schema_version"}
        })

    def to_state(self) -> JsonObject:
        return {
            "agent": self.agent,
            "agent_definition": self.agent_definition,
            "provider": self.provider,
            "model": self.model,
            "model_mode": self.model_mode,
            "context_window": self.context_window,
            "parent_thread_id": self.parent_thread_id,
            "workspace_root": self.workspace_root,
            "title": self.title,
        }

    def to_dict(self) -> JsonObject:
        return {"schema_version": self.schema_version, **self.to_state()}


class ThreadMetadataSink(Protocol):
    def save(self, metadata: ThreadMetadata) -> None: ...


class ThreadMetadataState:
    """Own one immutable metadata value and persist every replacement."""

    def __init__(
        self,
        value: ThreadMetadata | None = None,
        *,
        sink: ThreadMetadataSink | None = None,
    ) -> None:
        self._value = value or ThreadMetadata()
        self._sink = sink

    @property
    def value(self) -> ThreadMetadata:
        return self._value

    def replace(self, value: ThreadMetadata) -> None:
        if value == self._value:
            return
        if self._sink is not None:
            self._sink.save(value)
        self._value = value


def _optional_string(value: Mapping[str, object], name: str) -> str:
    raw = value.get(name, "")
    if not isinstance(raw, str):
        raise TypeError(f"{name} must be a string")
    return raw


def _optional_integer(value: Mapping[str, object], name: str) -> int:
    return _integer(value.get(name, 0), name)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"{name} must be a non-negative integer")
    return value


__all__ = [
    "THREAD_METADATA_SCHEMA_VERSION",
    "ThreadMetadata",
    "ThreadMetadataSink",
    "ThreadMetadataState",
]
