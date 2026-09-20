"""Typed task-list state and query contract shared by the plugin and carriers.

The task tools mirror Claude Code's task system: every task is an addressable
entity with an incrementing id, explicit dependency edges, and an owner slot,
instead of one flat list replaced wholesale.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from XBotv2.core.operations import EmptyRequest, Operation

TASK_SCHEMA_VERSION = 2
TaskStatus = Literal["pending", "in_progress", "completed"]
TASK_STATUSES = frozenset({"pending", "in_progress", "completed"})
# ``deleted`` is an update instruction, never a stored status.
TaskStatusInput = Literal["pending", "in_progress", "completed", "deleted"]
TASK_STATUS_INPUTS = frozenset({"pending", "in_progress", "completed", "deleted"})

class TaskConfig(BaseModel):
    """Guardrails owned by this plugin, overridable per mount.

    These keep one thread's list from growing into unbounded model context;
    they are not part of the upstream tool contract.
    """

    model_config = ConfigDict(extra="forbid")

    max_tasks: int = Field(default=50, ge=1)
    max_subject_chars: int = Field(default=500, ge=1)
    max_description_chars: int = Field(default=2_000, ge=1)
    max_active_form_chars: int = Field(default=200, ge=1)
    #: Turns without a task mutation before the reminder nudges the model.
    reminder_after_turns: int = Field(default=3, ge=1)
    #: Append the "closed a plan without verifying" nudge on the last completion.
    verification_nudge: bool = True
    #: Case-insensitive regex marking a task as the verification step.
    verification_hint: str = "verif"


class TaskValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Task(BaseModel):
    """One addressable task, matching Claude Code's task entity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    description: str = ""
    activeForm: str = Field(default="", exclude_if=lambda value: not value)
    owner: str = ""
    status: TaskStatus = "pending"
    blocks: tuple[str, ...] = ()
    blockedBy: tuple[str, ...] = ()
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    def display_label(self) -> str:
        """The label a client shows while this task runs."""
        if self.status == "in_progress" and self.activeForm:
            return self.activeForm
        return self.subject


class TaskList(BaseModel):
    """The whole thread task list plus its id high-watermark."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = TASK_SCHEMA_VERSION
    #: Next id to hand out. It never moves backwards, so a deleted id is not
    #: reused — the same guarantee Claude Code keeps with its watermark file.
    next_id: int = Field(default=1, ge=1)
    tasks: tuple[Task, ...] = ()

    @classmethod
    def from_items(cls, items: Iterable[Task]) -> "TaskList":
        tasks = tuple(items)
        return cls(
            next_id=max((_numeric_id(task.id) for task in tasks), default=0) + 1,
            tasks=tasks,
        )

    def find(self, task_id: str) -> Task | None:
        return next((task for task in self.tasks if task.id == task_id), None)

    def allocate_id(self) -> str:
        return str(self.next_id)

    def replace(self, task: Task) -> "TaskList":
        tasks = tuple(task if existing.id == task.id else existing for existing in self.tasks)
        if not any(existing.id == task.id for existing in self.tasks):
            tasks = (*self.tasks, task)
        return self.model_copy(update={"tasks": tasks})

    def remove(self, task_id: str) -> "TaskList":
        """Delete one task and every dependency edge that referenced it."""
        tasks = tuple(
            _without_edge(
                task,
                blocks=task_id,
                blocked_by=task_id,
            )
            for task in self.tasks
            if task.id != task_id
        )
        return self.model_copy(update={"tasks": tasks})

    def projection(self) -> dict[str, JsonValue]:
        """Client-facing payload; the ``kind`` tag keeps its wire name."""
        return {"kind": "todo_snapshot", **self.model_dump(mode="json")}


def _without_edge(task: Task, *, blocks: str, blocked_by: str) -> Task:
    return task.model_copy(update={
        "blocks": tuple(value for value in task.blocks if value != blocks),
        "blockedBy": tuple(value for value in task.blockedBy if value != blocked_by),
    })


def _numeric_id(task_id: str) -> int:
    try:
        return int(task_id)
    except ValueError:
        return 0


def parse_task_status(value: object, *, allow_deleted: bool) -> TaskStatusInput:
    """Validate one status argument against the tool contract."""
    if not isinstance(value, str) or value not in TASK_STATUS_INPUTS:
        raise TaskValidationError(
            "invalid_task_status",
            f"Unsupported task status: {value!r}",
        )
    if value == "deleted" and not allow_deleted:
        raise TaskValidationError(
            "invalid_task_status",
            "Stored tasks cannot carry the deleted status",
        )
    return cast(TaskStatusInput, value)


def parse_task_text(
    value: object,
    *,
    field: str,
    limit: int,
    required: bool,
) -> str:
    """Validate, trim, and bound one model-authored text field."""
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise TaskValidationError(
            "invalid_task",
            f"Task {field} must be a string",
        )
    if required and not text:
        raise TaskValidationError(
            "invalid_task",
            f"Task {field} must not be empty",
        )
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
        raise TaskValidationError(
            "invalid_task_id",
            "A task id is required",
        )
    return value.strip()


def parse_task_ids(value: object, *, field: str) -> tuple[str, ...]:
    """Validate one id list argument (``addBlocks``/``addBlockedBy``)."""
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TaskValidationError(
            "invalid_task_ids",
            f"Task {field} must be a list of task ids",
        )
    ids: list[str] = []
    for entry in value:
        parsed = parse_task_id(entry)
        if parsed not in ids:
            ids.append(parsed)
    return tuple(ids)


GET_TODOS = Operation[EmptyRequest, TaskList](
    "todolist/snapshot/get",
    EmptyRequest,
    TaskList,
)


__all__ = [
    "GET_TODOS",
    "TASK_SCHEMA_VERSION",
    "TASK_STATUSES",
    "TASK_STATUS_INPUTS",
    "Task",
    "TaskConfig",
    "TaskList",
    "TaskStatus",
    "TaskStatusInput",
    "TaskValidationError",
    "parse_task_id",
    "parse_task_ids",
    "parse_task_status",
    "parse_task_text",
]
