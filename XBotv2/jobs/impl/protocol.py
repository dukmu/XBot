
from XBotv2.agentloop.api.contracts import AGENT_LOOP
"""HTTP job routes."""

from fastapi import APIRouter
from XBotv2.core.api.operations import EmptyRequest
from XBotv2.jobs.api.contracts import LIST_JOBS, STOP_ALL_JOBS, STOP_JOB, StopJob
from XBotv2.jobs.api.protocol import JobListResponse, JobStopResponse
from XBotv2.session.api.contracts import SessionsPort


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
        result = await (await sessions.locate(session_id, thread_id)).require(AGENT_LOOP).invoke(LIST_JOBS, EmptyRequest())
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
        result = await (await sessions.locate(session_id, thread_id)).require(AGENT_LOOP).invoke(STOP_JOB, StopJob(job_id))
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
        result = await (await sessions.locate(session_id, thread_id)).require(AGENT_LOOP).invoke(STOP_ALL_JOBS, EmptyRequest())
        return JobStopResponse(
            session_id=session_id,
            thread_id=thread_id,
            matched_count=len(result.jobs),
            jobs=list(result.jobs),
        )

    return router
