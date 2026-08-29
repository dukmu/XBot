"""Typed Todo query contract shared by the plugin and carriers."""

from XBotv2.core.operations import EmptyRequest, Operation
from XBotv2.todolist.models import TodoSnapshot


GET_TODOS = Operation[EmptyRequest, TodoSnapshot](
    "todolist/snapshot/get",
    EmptyRequest,
    TodoSnapshot,
)


__all__ = ["GET_TODOS"]
