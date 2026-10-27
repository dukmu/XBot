"""Owner-defined jobs use one explicit lifecycle state machine."""

import asyncio
from dataclasses import dataclass

import pytest

from XBotv2.jobs.api.contracts import JOB_COMPLETED, JOB_UPDATED, JobNotFound, JobRegistryClosed
from XBotv2.jobs.impl.registry import JobRegistry
from XBotv2.agentloop.api.contracts import HumanInput, InboxItem, RuntimeInput
from XBotv2.core.api.domain import InboxTarget


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
async def test_cancel_before_runner_is_scheduled_completes_each_job_once():
    class _Publisher:
        def __init__(self):
            self.events = []

        async def emit(self, name, view):
            self.events.append((name, view.id, view.state))

    publisher = _Publisher()
    registry = JobRegistry(publisher=publisher)
    first = await registry.create(spec=_Spec(kind="subagent"), owner="test")
    second = await registry.create(spec=_Spec(kind="subagent"), owner="test")
    registry.start(first.id, _Runner())
    registry.start(second.id, _Runner())

    # Do not yield between ``start`` and the first cancellation: this is the
    # shutdown window where the scheduled coroutine has not entered _execute.
    results = [
        await registry.cancel(first.id),
        await registry.cancel(second.id),
    ]
    waited = await registry.wait([first.id, second.id], timeout=0.1)

    assert all(result.cancelled for result in results)
    assert first.status == "cancelled_before_start"
    assert second.status in {"cancelled_before_start", "cancelled_running"}
    assert waited.pending == ()
    assert {view.id for view in waited.ready} == {first.id, second.id}
    for job in (first, second):
        terminal_updates = [
            event for event in publisher.events
            if event == (JOB_UPDATED, job.id, job.status)
        ]
        completions = [
            event for event in publisher.events
            if event == (JOB_COMPLETED, job.id, job.status)
        ]
        assert len(terminal_updates) == 1
        assert len(completions) == 1


@pytest.mark.asyncio
async def test_cancel_does_not_overwrite_result_that_finishes_during_cancel():
    class _FinishesOnCancel(_Runner):
        async def run(self, job):
            try:
                return await super().run(job)
            except asyncio.CancelledError:
                await self.release.wait()
                return self.result

        async def cancel(self, _job):
            self.release.set()
            await asyncio.sleep(0)

    registry = JobRegistry()
    job = await registry.create(spec=_Spec(), owner="test")
    runner = _FinishesOnCancel(_Result("won race"))
    registry.start(job.id, runner)
    await runner.started.wait()

    result = await registry.cancel(job.id)

    assert result.cancelled is False
    assert job.status == "succeeded"
    assert job.result == _Result("won race")


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


@pytest.mark.asyncio
async def test_any_wait_returns_for_already_completed_target():
    registry = JobRegistry()
    first = await registry.create(spec=_Spec(), owner="test")
    second = await registry.create(spec=_Spec(), owner="test")
    runner = _Runner()
    registry.start(first.id, runner)
    runner.release.set()
    await registry.wait([first.id], timeout=1)
    registry.start(second.id, _Runner())
    result = await registry.wait([first.id, second.id], mode="any", timeout=0.1)
    assert [view.id for view in result.ready] == [first.id]
    assert result.pending == (second.id,)
    assert not result.timed_out
    await registry.shutdown()


@pytest.mark.asyncio
async def test_completed_jobs_are_bounded_and_running_wait_keeps_its_result():
    registry = JobRegistry()
    first = await registry.create(spec=_Spec(), owner="test")
    runner = _Runner()
    registry.start(first.id, runner)
    waiter = asyncio.create_task(registry.wait([first.id], timeout=2))
    await asyncio.sleep(0)
    for _ in range(101):
        job = await registry.create(spec=_Spec(), owner="test")
        await registry.cancel(job.id)
    runner.release.set()
    assert (await waiter).ready[0].id == first.id
    assert len(registry.all()) == 100
    with pytest.raises(JobNotFound):
        await registry.wait(["probe_2"], timeout=0)
    await registry.shutdown()


@pytest.mark.asyncio
async def test_wait_observes_unclaimed_steer_but_ignores_own_completion():
    class Inbox:
        pending = []

        def __init__(self):
            self.observers = set()

        def subscribe(self):
            event = asyncio.Event()
            self.observers.add(event)
            return event

        def unsubscribe(self, event):
            self.observers.remove(event)

        def send(self, item):
            self.pending.append(item)
            for event in self.observers:
                event.set()

    registry = JobRegistry()
    inbox = Inbox()
    job = await registry.create(spec=_Spec(), owner="test")
    runner = _Runner()
    registry.start(job.id, runner)
    inbox.send(InboxItem(target=InboxTarget.NEXT_STEP, input=RuntimeInput(
        source=job.id, event="job/completed", content="done",
    )))
    waiter = asyncio.create_task(registry.wait([job.id], inbox=inbox))
    await asyncio.sleep(0)
    assert not waiter.done()
    inbox.send(InboxItem(target=InboxTarget.NEXT_STEP, input=HumanInput(content="stop")))
    assert (await waiter).reason == "steered"
    assert not inbox.observers
    await registry.shutdown()


@pytest.mark.asyncio
async def test_cancelling_wait_does_not_cancel_job():
    registry = JobRegistry()
    job = await registry.create(spec=_Spec(), owner="test")
    runner = _Runner()
    registry.start(job.id, runner)
    await runner.started.wait()
    waiter = asyncio.create_task(registry.wait([job.id]))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert job.status == "running"
    runner.release.set()
    assert (await registry.wait([job.id], timeout=1)).reason == "ready"
    await registry.shutdown()
