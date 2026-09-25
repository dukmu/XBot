"""Owner-defined jobs use one explicit lifecycle state machine."""

import asyncio
from dataclasses import dataclass

import pytest

from XBotv2.jobs import JobRegistryClosed
from XBotv2.jobs.registry import JobRegistry


@dataclass(frozen=True)
class _Spec:
    kind: str = "probe"
    label: str = "Probe job"


@dataclass(frozen=True)
class _Result:
    value: str


class _Runner:
    def __init__(self, result=None, error=None):
        self.result = result or _Result("complete")
        self.error = error
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, _job):
        self.started.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.result

    async def cancel(self, _job):
        return None
async def test_job_transitions_from_queued_to_running_to_succeeded():
    registry = JobRegistry()
    job = await registry.create(spec=_Spec(), owner="test")
    runner = _Runner(_Result("done"))
    registry.start(job.id, runner)

    await runner.started.wait()
    assert job.status == "running"
    runner.release.set()
    result = await registry.wait([job.id], timeout=1)

    assert result.pending == ()
    assert result.ready[0].state == "succeeded"
    assert result.ready[0].summary is None


@pytest.mark.asyncio
async def test_job_failure_and_cancellation_are_distinct_terminal_states():
    registry = JobRegistry()
    failed = await registry.create(spec=_Spec(), owner="test")
    failure = _Runner(error=ValueError("broken"))
    registry.start(failed.id, failure)
    await failure.started.wait()
    failure.release.set()
    await registry.wait([failed.id], timeout=1)
    assert failed.status == "failed_running"
    assert registry.view(failed).summary == "broken"

    cancelled = await registry.create(spec=_Spec(), owner="test")
    blocked = _Runner()
    registry.start(cancelled.id, blocked)
    await blocked.started.wait()
    outcome = await registry.cancel(cancelled.id)
    assert outcome.cancelled
    assert cancelled.status == "cancelled_running"


@pytest.mark.asyncio
async def test_kind_limit_serializes_job_execution():
    registry = JobRegistry(limits={"probe": 1})
    first = await registry.create(spec=_Spec(), owner="test")
    second = await registry.create(spec=_Spec(), owner="test")
    first_runner = _Runner()
    second_runner = _Runner()
    registry.start(first.id, first_runner)
    registry.start(second.id, second_runner)

    await first_runner.started.wait()
    await asyncio.sleep(0)
    assert not second_runner.started.is_set()
    first_runner.release.set()
    await second_runner.started.wait()
    second_runner.release.set()
    await registry.wait([first.id, second.id], timeout=1)


@pytest.mark.asyncio
async def test_shutdown_cancels_jobs_and_rejects_new_ones():
    registry = JobRegistry()
    job = await registry.create(spec=_Spec(), owner="test")
    runner = _Runner()
    registry.start(job.id, runner)
    await runner.started.wait()

    stopped = await registry.shutdown()
    assert stopped[0].state == "cancelled_running"
    assert registry.all() == []
    with pytest.raises(JobRegistryClosed):
        await registry.create(spec=_Spec(), owner="test")
