"""Task list snapshot operation."""

from pydantic import BaseModel, ConfigDict, Field

from XBotv2.core.api.operations import EmptyRequest, Operation
from XBotv2.todolist.api.models import Task, TaskList

GET_TODOS = Operation[EmptyRequest, TaskList](
    "todolist/snapshot/get",
    EmptyRequest,
    TaskList,
)


class CompletedTasksRequest(BaseModel):
    after: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=400)
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompletedTasksPage(BaseModel):
    tasks: tuple[Task, ...]
    next_after: int | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)


GET_COMPLETED_TODOS = Operation[CompletedTasksRequest, CompletedTasksPage](
    "todolist/completed/page",
    CompletedTasksRequest,
    CompletedTasksPage,
)
