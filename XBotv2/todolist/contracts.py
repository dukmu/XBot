"""Canonical task-list state and operations."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from XBotv2.core.operations import EmptyRequest, Operation

TaskStatus = Literal["pending", "in_progress", "completed"]
TaskStatusInput = TaskStatus
TASK_STATUSES = frozenset({"pending", "in_progress", "completed"})


class TaskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tasks: int = Field(default=50, ge=1)
    max_subject_chars: int = Field(default=500, ge=1)
    max_description_chars: int = Field(default=2_000, ge=1)
    max_active_form_chars: int = Field(default=200, ge=1)
    reminder_after_turns: int = Field(default=3, ge=1)
    verification_nudge: bool = True
    verification_hint: str = "verif"


class TaskValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Task(BaseModel):
    id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    description: str = ""
    active_form: str = Field(default="", alias="activeForm")
    owner: str = ""
    status: TaskStatus = "pending"
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    def display_label(self) -> str:
        if self.status == "in_progress" and self.active_form:
            return self.active_form
        return self.subject


class DependencyEdge(BaseModel):
    prerequisite: str = Field(min_length=1)
    dependent: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskList(BaseModel):
    version: int = Field(default=0, ge=0)
    next_id: int = Field(default=1, ge=1)
    tasks: tuple[Task, ...] = ()
    edges: tuple[DependencyEdge, ...] = ()
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _validate_graph(self) -> "TaskList":
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("Task ids must be unique")
        known = set(ids)
        edge_keys = {
            (edge.prerequisite, edge.dependent) for edge in self.edges
        }
        if len(edge_keys) != len(self.edges):
            raise ValueError("Dependency edges must be unique")
        for edge in self.edges:
            if edge.prerequisite == edge.dependent:
                raise ValueError("A task cannot depend on itself")
            if edge.prerequisite not in known or edge.dependent not in known:
                raise ValueError("Dependency edges must reference existing tasks")
        if _has_cycle(self.edges):
            raise ValueError("Task dependencies must be acyclic")
        return self

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
        if self.find(task.id) is None:
            raise ValueError(f"Unknown task: {task.id}")
        tasks = tuple(
            task if existing.id == task.id else existing
            for existing in self.tasks
        )
        return self.model_copy(update={"version": self.version + 1, "tasks": tasks})

    def append(self, task: Task) -> "TaskList":
        if self.find(task.id) is not None:
            raise ValueError(f"Duplicate task: {task.id}")
        return self.model_copy(update={
            "version": self.version + 1,
            "next_id": self.next_id + 1,
            "tasks": (*self.tasks, task),
        })

    def remove(self, task_id: str) -> "TaskList":
        if self.find(task_id) is None:
            raise ValueError(f"Unknown task: {task_id}")
        return self.model_copy(update={
            "version": self.version + 1,
            "tasks": tuple(task for task in self.tasks if task.id != task_id),
            "edges": tuple(
                edge for edge in self.edges
                if task_id not in {edge.prerequisite, edge.dependent}
            ),
        })

    def add_edge(self, edge: DependencyEdge) -> "TaskList":
        if edge in self.edges:
            return self
        return TaskList(
            version=self.version + 1,
            next_id=self.next_id,
            tasks=self.tasks,
            edges=(*self.edges, edge),
        )

    def prerequisites(self, task_id: str) -> tuple[str, ...]:
        return _sort_ids(
            edge.prerequisite
            for edge in self.edges
            if edge.dependent == task_id
        )

    def dependents(self, task_id: str) -> tuple[str, ...]:
        return _sort_ids(
            edge.dependent
            for edge in self.edges
            if edge.prerequisite == task_id
        )


class TaskChanged(BaseModel):
    kind: Literal["task_changed"] = "task_changed"
    snapshot: TaskList
    model_config = ConfigDict(extra="forbid", frozen=True)


def _has_cycle(edges: Sequence[DependencyEdge]) -> bool:
    graph: dict[str, set[str]] = {}
    for edge in edges:
        graph.setdefault(edge.prerequisite, set()).add(edge.dependent)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(next_node) for next_node in graph.get(node, ())):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)


def _sort_ids(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value)))


def _numeric_id(task_id: str) -> int:
    return int(task_id) if task_id.isdigit() else 0


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


GET_TODOS = Operation[EmptyRequest, TaskList](
    "todolist/snapshot/get",
    EmptyRequest,
    TaskList,
)


__all__ = [
    "DependencyEdge",
    "GET_TODOS",
    "TASK_STATUSES",
    "Task",
    "TaskChanged",
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
