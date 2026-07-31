from __future__ import annotations

import random
import time
from dataclasses import dataclass, replace

from flakyqueue.clock import SystemClock
from flakyqueue.store import MemoryStore


@dataclass(frozen=True)
class Task:
    id: str
    priority: int
    created_at: float
    attempts: int = 0
    run_at: float = 0.0


class Scheduler:
    def __init__(self, clock=None, store=None, random_source=None) -> None:
        self.clock = clock or SystemClock()
        self.store = store or MemoryStore()
        self.random_source = random_source

    def add(self, task_id: str, priority: int) -> Task:
        task = Task(id=task_id, priority=priority, created_at=time.time(), run_at=time.time())
        self.store.save(task)
        return task

    def ready(self):
        now = time.time()
        items = [task for task in self.store.all() if task.run_at <= now]
        random.shuffle(items)
        return sorted(items, key=lambda task: task.priority, reverse=True)

    def schedule_retry(self, task: Task, base_delay: float = 5.0) -> Task:
        jitter = random.random()
        updated = replace(task, attempts=task.attempts + 1, run_at=time.time() + base_delay + jitter)
        self.store.save(updated)
        return updated
