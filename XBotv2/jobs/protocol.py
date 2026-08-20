"""Background task routes: list, stop one, and stop all."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import Field
from XBotv2.core.operations import EmptyRequest
from XBotv2.core.tools import ClientEvent
from XBotv2.jobs.contracts import (
    LIST_TASKS,
    STOP_ALL_TASKS,
    STOP_TASK,
    StopTask,
    TaskSnapshot,
)
from XBotv2.protocol import WireModel
from XBotv2.server import contribute_router
from XBotv2.session import SessionRef, dispatch_session_operation


class TaskUpdatedData(WireModel):
    task_id: str = Field(min_length=1)
    kind: Literal["shell", "agent"] = "shell"
    command: str = Field(min_length=1)
    cwd: str
    status: Literal["pending", "running", "completed", "failed", "stopped"]
    created_at: float = Field(ge=0)
    started_at: float = Field(ge=0)
    finished_at: float = Field(ge=0)
    output: str = ""
    error: str = ""
    agent: str = ""
    thread_id: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)


class TaskCompletionData(WireModel):
    type: Literal["background_task", "subagent"]
    kind: Literal["background_task", "subagent"]
    task_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    command: str = ""
    agent: str = ""


def task_updated_event(task: TaskSnapshot) -> ClientEvent:
    payload = TaskUpdatedData.model_validate(asdict(task))
    return ClientEvent("task_updated", payload.model_dump())


def task_completion_event(task: TaskSnapshot) -> ClientEvent:
    kind = "background_task" if task.kind == "shell" else "subagent"
    payload = TaskCompletionData(
        type=kind,
        kind=kind,
        task_id=task.task_id,
        status=task.status,
        command=task.command,
        agent=task.agent,
    )
    return ClientEvent("completion_notice", payload.model_dump())


class TaskListResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    tasks: list[TaskUpdatedData] = Field(default_factory=list)


class TaskStopResponse(TaskListResponse):
    matched_count: int = Field(ge=0)


def build_tasks_router(*, events: Any) -> APIRouter:
    """Background task control routes backed by the session jobs registry."""

    router = APIRouter()

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/tasks",
        operation_id="list_tasks",
    )
    async def list_tasks_endpoint(
        session_id: str,
        thread_id: str,
    ) -> TaskListResponse:
        result = await dispatch_session_operation(
            events,
            SessionRef(session_id, thread_id),
            LIST_TASKS,
            EmptyRequest(),
        )
        return TaskListResponse(
            session_id=session_id,
            thread_id=thread_id,
            tasks=[asdict(task) for task in result.tasks],
        )

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/tasks/{task_id}/stop",
        operation_id="stop_task",
    )
    async def stop_task_endpoint(
        session_id: str,
        thread_id: str,
        task_id: str,
    ) -> TaskStopResponse:
        result = await dispatch_session_operation(
            events,
            SessionRef(session_id, thread_id),
            STOP_TASK,
            StopTask(task_id),
        )
        return TaskStopResponse(
            session_id=session_id,
            thread_id=thread_id,
            matched_count=1,
            tasks=[asdict(task) for task in result.tasks],
        )

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/tasks/stop",
        operation_id="stop_all_tasks",
    )
    async def stop_all_tasks_endpoint(
        session_id: str,
        thread_id: str,
    ) -> TaskStopResponse:
        result = await dispatch_session_operation(
            events,
            SessionRef(session_id, thread_id),
            STOP_ALL_TASKS,
            EmptyRequest(),
        )
        return TaskStopResponse(
            session_id=session_id,
            thread_id=thread_id,
            matched_count=len(result.tasks),
            tasks=[asdict(task) for task in result.tasks],
        )

    return router


class JobsProtocolPlugin:
    """Contribute task routes through the XCore route event."""

    inject = ["server"]
    name = "xbot.protocol.jobs"

    async def apply(self, ctx: Any, config: Any = None) -> None:
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_tasks_router(events=ctx),
        )


plugin = JobsProtocolPlugin()


__all__ = [
    "TaskCompletionData",
    "TaskListResponse",
    "TaskStopResponse",
    "TaskUpdatedData",
    "build_tasks_router",
    "task_completion_event",
    "task_updated_event",
]
