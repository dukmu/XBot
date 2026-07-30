from flakyqueue.scheduler import Scheduler


class FakeClock:
    def __init__(self):
        self.value = 200.0

    def now(self):
        return self.value


class FakeRandom:
    def __init__(self, values):
        self.values = list(values)

    def random(self):
        return self.values.pop(0)


def test_retry_jitter_uses_injected_random_source():
    scheduler = Scheduler(clock=FakeClock(), random_source=FakeRandom([0.25]))
    task = scheduler.add("task-retry", priority=3)
    retry = scheduler.schedule_retry(task, base_delay=10.0)
    assert retry.attempts == 1
    assert retry.run_at == 210.25


def test_retry_task_is_not_ready_until_clock_reaches_run_at():
    clock = FakeClock()
    scheduler = Scheduler(clock=clock, random_source=FakeRandom([0.5]))
    task = scheduler.add("task-wait", priority=3)
    retry = scheduler.schedule_retry(task, base_delay=10.0)
    assert scheduler.ready() == []
    clock.value = retry.run_at
    assert [task.id for task in scheduler.ready()] == ["task-wait"]
