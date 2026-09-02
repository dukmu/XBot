"""Strict persisted and client-facing Todo state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, JsonValue, field_validator

TODO_SCHEMA_VERSION = 1
TodoStatus = Literal["pending", "in_progress", "completed"]
TODO_STATUSES = frozenset({"pending", "in_progress", "completed"})


class TodoValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TodoItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str
    status: TodoStatus

    @classmethod
    def parse_input(cls, value: Mapping[str, object]) -> "TodoItem":
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
        return cls(content=content.strip(), status=cast(TodoStatus, status))

class TodoSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = TODO_SCHEMA_VERSION
    items: tuple[TodoItem, ...] = ()

    @classmethod
    def from_items(cls, items: Sequence[Mapping[str, object]]) -> "TodoSnapshot":
        return cls(items=tuple(TodoItem.parse_input(item) for item in items))

    @field_validator("items", mode="before")
    @classmethod
    def _validate_items(cls, value: object) -> tuple[TodoItem, ...]:
        if not isinstance(value, (list, tuple)):
            raise TypeError("TodoSnapshot.items must be a list")
        return tuple(
            item if isinstance(item, TodoItem) else TodoItem.parse_input(item)
            for item in value
        )

    def projection(self) -> dict[str, JsonValue]:
        return {"kind": "todo_snapshot", **self.model_dump(mode="json")}


__all__ = [
    "TODO_SCHEMA_VERSION",
    "TODO_STATUSES",
    "TodoItem",
    "TodoSnapshot",
    "TodoStatus",
    "TodoValidationError",
]
