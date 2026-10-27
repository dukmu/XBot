"""Task list snapshot operation."""
from XBotv2.core.api.operations import EmptyRequest, Operation
from XBotv2.todolist.api.models import TaskList

GET_TODOS = Operation[EmptyRequest, TaskList](
    "todolist/snapshot/get",
    EmptyRequest,
    TaskList,
)
