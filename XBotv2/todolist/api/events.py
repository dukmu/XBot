"""Task list change event."""
from typing import Literal
from pydantic import BaseModel, ConfigDict
from XBotv2.todolist.api.models import TaskList

class TaskChanged(BaseModel):
    kind: Literal["task_changed"] = "task_changed"
    snapshot: TaskList
    model_config = ConfigDict(extra="forbid", frozen=True)
