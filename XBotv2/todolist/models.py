"""Strict persisted and client-facing Todo state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from XBotv2.core.tools import JsonObject

TODO_SCHEMA_VERSION = 1
TodoStatus = Literal["pending", "in_progress", "completed"]
TODO_STATUSES = frozenset({"pending", "in_progress", "completed"})


class TodoValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TodoItem:
    content: str
    status: TodoStatus

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "TodoItem":
        if set(value) != {"content", "status"}:
            raise TodoValidationError(
                "invalid_todos", "Todo items must contain only content and status"
            )
        content = value["content"]
        status = value["status"]
        if not isinstance(content, str) or not content.strip():
            raise TodoValidationError(
                "invalid_todo", "Todo item content must be a non-empty string"
            )
        if status not in TODO_STATUSES:
            raise TodoValidationError(
                "invalid_todo_status", f"Unsupported Todo status: {status!r}"
            )
        return cls(content.strip(), cast(TodoStatus, status))

    def to_dict(self) -> JsonObject:
        return {"content": self.content, "status": self.status}


@dataclass(frozen=True, slots=True)
class TodoSnapshot:
    schema_version: int = TODO_SCHEMA_VERSION
    items: tuple[TodoItem, ...] = ()

    @classmethod
    def from_items(cls, items: Sequence[Mapping[str, object]]) -> "TodoSnapshot":
        return cls(items=tuple(TodoItem.from_mapping(item) for item in items))

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "TodoSnapshot":
        if set(value) != {"schema_version", "items"}:
            raise ValueError("TodoSnapshot contains unknown or missing fields")
        version = value["schema_version"]
        if type(version) is not int or version != TODO_SCHEMA_VERSION:
            raise ValueError(f"Unsupported TodoSnapshot schema version: {version!r}")
        items = value["items"]
        if not isinstance(items, list):
            raise TypeError("TodoSnapshot.items must be a list")
        if not all(isinstance(item, Mapping) for item in items):
            raise TypeError("TodoSnapshot items must be objects")
        return cls.from_items(items)

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "items": [item.to_dict() for item in self.items],
        }

    def projection(self) -> JsonObject:
        return {"kind": "todo_snapshot", **self.to_dict()}


__all__ = [
    "TODO_SCHEMA_VERSION",
    "TODO_STATUSES",
    "TodoItem",
    "TodoSnapshot",
    "TodoStatus",
    "TodoValidationError",
]
