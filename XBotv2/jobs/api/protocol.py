"""Job wire events and response models."""

from typing import Literal
from pydantic import Field
from XBotv2.jobs.api.contracts import JobView
from XBotv2.protocol.api.models import WireModel


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
