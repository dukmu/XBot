from flakyqueue.scheduler import Scheduler


class FakeClock:
    def __init__(self):
        self.value = 1000.0

    def now(self):
        return self.value


class FakeRandom:
    def __init__(self, values):
        self.values = list(values)

    def random(self):
        return self.values.pop(0)


def test_ready_order_is_stable_for_equal_priority():
    clock = FakeClock()
    scheduler = Scheduler(clock=clock, random_source=FakeRandom([0.3]))
    scheduler.add("task-b", priority=5)
    scheduler.add("task-a", priority=5)
    scheduler.add("task-c", priority=9)
    assert [task.id for task in scheduler.ready()] == ["task-c", "task-a", "task-b"]
    assert [task.id for task in scheduler.ready()] == ["task-c", "task-a", "task-b"]


def test_add_uses_injected_clock():
    clock = FakeClock()
    scheduler = Scheduler(clock=clock, random_source=FakeRandom([0.1]))
    task = scheduler.add("task-1", priority=1)
    assert task.created_at == 1000.0
    assert task.run_at == 1000.0
