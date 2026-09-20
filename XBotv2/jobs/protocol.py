"""Background task routes: list, stop one, and stop all."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import Field
from XBotv2.core.operations import EmptyRequest
from XBotv2.core.tools import ClientEvent
from XBotv2.jobs.contracts import (
    LIST_JOBS,
    STOP_ALL_JOBS,
    STOP_JOB,
    StopJob,
    JobSnapshot,
)
from XBotv2.protocol import WireModel
from XBotv2.session.contracts import SessionsPort


class JobCompletionData(WireModel):
    type: Literal["background_job", "subagent"]
    kind: Literal["background_job", "subagent"]
    job_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    command: str = ""
    agent: str = ""


def job_updated_event(snapshot: JobSnapshot) -> ClientEvent:
    return ClientEvent(type="job_updated", data=snapshot.model_dump(mode="json"))


def job_completion_event(snapshot: JobSnapshot) -> ClientEvent:
    kind = "background_job" if snapshot.kind == "shell" else "subagent"
    payload = JobCompletionData(
        type=kind,
        kind=kind,
        job_id=snapshot.job_id,
        status=snapshot.status,
        command=snapshot.command,
        agent=snapshot.agent,
    )
    return ClientEvent(
        type="completion_notice",
        data=payload.model_dump(mode="json"),
    )


class JobListResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    jobs: list[JobSnapshot] = Field(default_factory=list)


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
    "JobCompletionData",
    "JobListResponse",
    "JobStopResponse",
    "build_jobs_router",
    "job_completion_event",
    "job_updated_event",
]
