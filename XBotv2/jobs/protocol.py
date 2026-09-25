"""Background task routes: list, stop one, and stop all."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import Field
from XBotv2.core.operations import EmptyRequest
from XBotv2.jobs.contracts import (
    LIST_JOBS,
    STOP_ALL_JOBS,
    STOP_JOB,
    StopJob,
    JobView,
)
from XBotv2.protocol import WireModel
from XBotv2.session.contracts import SessionsPort


class JobUpdatedEvent(WireModel):
    kind: Literal["job_updated"] = "job_updated"
    view: JobView


class JobCompletedEvent(WireModel):
    kind: Literal["job_completed"] = "job_completed"
    view: JobView


def job_updated_event(view: JobView) -> JobUpdatedEvent:
    return JobUpdatedEvent(view=view)


def job_completed_event(view: JobView) -> JobCompletedEvent:
    return JobCompletedEvent(view=view)


class JobListResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    jobs: list[JobView] = Field(default_factory=list)


class JobStopResponse(JobListResponse):
    matched_count: int = Field(ge=0)


def build_jobs_router(*, sessions: SessionsPort) -> APIRouter:
    """Background job control routes backed by the session jobs registry."""

    router = APIRouter()

    @router.get(
        "/sessions/{session_id}/threads/{thread_id}/jobs",
        operation_id="list_jobs",
    )
    async def list_jobs_endpoint(
        session_id: str,
        thread_id: str,
    ) -> JobListResponse:
        result = await sessions.dispatch(
            session_id, thread_id, LIST_JOBS, EmptyRequest()
        )
        return JobListResponse(
            session_id=session_id,
            thread_id=thread_id,
            jobs=list(result.jobs),
        )

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/jobs/{job_id}/stop",
        operation_id="stop_job",
    )
    async def stop_job_endpoint(
        session_id: str,
        thread_id: str,
        job_id: str,
    ) -> JobStopResponse:
        result = await sessions.dispatch(
            session_id, thread_id, STOP_JOB, StopJob(job_id)
        )
        return JobStopResponse(
            session_id=session_id,
            thread_id=thread_id,
            matched_count=1,
            jobs=list(result.jobs),
        )

    @router.post(
        "/sessions/{session_id}/threads/{thread_id}/jobs/stop",
        operation_id="stop_all_jobs",
    )
    async def stop_all_jobs_endpoint(
        session_id: str,
        thread_id: str,
    ) -> JobStopResponse:
        result = await sessions.dispatch(
            session_id, thread_id, STOP_ALL_JOBS, EmptyRequest()
        )
        return JobStopResponse(
            session_id=session_id,
            thread_id=thread_id,
            matched_count=len(result.jobs),
            jobs=list(result.jobs),
        )

    return router


__all__ = [
    "JobCompletedEvent",
    "JobUpdatedEvent",
    "JobListResponse",
    "JobStopResponse",
    "build_jobs_router",
    "job_completed_event",
    "job_updated_event",
]
