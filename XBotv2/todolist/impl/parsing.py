"""Validate task tool arguments."""
from collections.abc import Sequence
from typing import cast
from XBotv2.todolist.api.models import (
    TASK_STATUSES,
    TaskStatusInput,
    TaskValidationError,
    _sort_ids,
)

def parse_task_status(value: object) -> TaskStatusInput:
    if not isinstance(value, str) or value not in TASK_STATUSES:
        raise TaskValidationError(
            "invalid_task_status",
            f"Unsupported task status: {value!r}",
        )
    return cast(TaskStatusInput, value)


def parse_task_text(
    value: object,
    *,
    field: str,
    limit: int,
    required: bool,
) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise TaskValidationError("invalid_task", f"Task {field} must be a string")
    if required and not text:
        raise TaskValidationError("invalid_task", f"Task {field} must not be empty")
    if len(text) > limit:
        raise TaskValidationError(
            "invalid_task",
            f"Task {field} must not exceed {limit} characters",
        )
    return text


def parse_task_id(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise TaskValidationError("invalid_task_id", "A task id is required")
    return value.strip()


def parse_task_ids(value: object, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TaskValidationError(
            "invalid_task_ids",
            f"Task {field} must be a list of task ids",
        )
    return _sort_ids(parse_task_id(entry) for entry in value)
