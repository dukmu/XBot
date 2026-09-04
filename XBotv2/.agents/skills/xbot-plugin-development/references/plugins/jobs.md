# `jobs`

Kind-agnostic background job lifecycle registry. Owns every lifecycle
concern shared by all job kinds: IDs, status transitions, waiting,
cancellation, event notification, result/output storage, and cleanup.

Domain adapters (shell, subagent) implement `JobRunner` and their
model-facing tools; they never hold job state themselves.

- **Import/profile:** `jobs`, Agent profile.
- **Source:** `XBotv2/jobs/plugin.py`,
  `XBotv2/jobs/registry.py`,
  `XBotv2/jobs/runner.py`,
  `XBotv2/jobs/output.py`,
  `XBotv2/jobs/commands.py`,
  `XBotv2/jobs/protocol.py`,
  `XBotv2/jobs/contracts.py`.
- **Injects/provides:** `commands`, `engine` → `jobs` (`JobRegistry`).
- **Subscribes to events:** `list/tasks` (`LIST_TASKS`),
  `stop/task` (`STOP_TASK`), `stop/all-tasks` (`STOP_ALL_TASKS`),
  `session/close`, `prepare/fork`.
- **Commands:** `/tasks` (list), `/task` (stop/stopall).

## Public data models

### `JobRegistry` (`XBotv2/jobs/registry.py:59-340`)

```python
class JobRegistry:
    def __init__(
        self,
        *,
        limits: dict[JobKind, int] | None = None,
        prefix: str = "job",
    ) -> None:
        self._jobs: dict[JobId, Job] = {}
        self._completion_events: dict[JobId, asyncio.Event] = {}
        self._runners: dict[JobId, JobRunner] = {}
        self._tasks: dict[JobId, asyncio.Task[None]] = {}
        self._next_id = 1
        self._prefix = prefix
        self._limits: dict[JobKind, asyncio.Semaphore] = {
            kind: asyncio.Semaphore(limit)
            for kind, limit in (limits or {}).items()
        }
        self._closing = False
        self.on_update: TaskCallback | None = None
        self.on_complete: TaskCallback | None = None

    @property
    def closing(self) -> bool: ...

    async def create(
        self,
        *,
        kind: JobKind,
        metadata: dict[str, JsonValue] | None = None,
        parent_job_id: JobId | None = None,
        name: str | None = None,
    ) -> Job: ...

    def start(self, job_id: JobId, runner: JobRunner) -> Job: ...

    def get(self, job_id: JobId) -> Job: ...
    def get_or_none(self, job_id: JobId) -> Job | None: ...
    def summary(self, job_id: JobId) -> JobSummary: ...
    def all(self) -> list[Job]: ...
    def is_busy(self) -> bool: ...

    def list(
        self,
        *,
        kind: JobKind | None = None,
        status: JobStatus | None = None,
        parent_job_id: JobId | None = None,
        recursive: bool = False,
        max_results: int = 20,
    ) -> list[JobSummary]: ...

    async def wait(
        self,
        ids: list[JobId],
        *,
        mode: WaitMode = "all",
        timeout: float | None = None,
    ) -> WaitResult: ...

    async def cancel(self, job_id: JobId) -> CancelResult: ...
    def remove(self, job_id: JobId) -> None: ...

    async def stop_all(self) -> list[TaskSnapshot]: ...
    async def shutdown(self) -> list[TaskSnapshot]: ...
    def remove_all(self) -> None: ...

    def snapshot(self, job: Job, *, full_output: bool = False) -> TaskSnapshot: ...
    def snapshots(self) -> list[TaskSnapshot]: ...

    # Internal execution
    async def _execute(self, job: Job, runner: JobRunner) -> None: ...
    async def _finish(self, job: Job, status: JobStatus) -> None: ...
    async def _notify_update(self, job: Job) -> None: ...
    async def _notify_complete(self, job: Job) -> None: ...
```

### `Job` dataclass

```python
@dataclass(slots=True)
class Job:
    id: JobId
    kind: JobKind
    status: JobStatus = JobStatus.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    parent_job_id: JobId | None = None
    name: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    result: JobResult | None = None
    error: JobError | None = None

    @property
    def terminal(self) -> bool: ...
    @property
    def elapsed_ms(self) -> int: ...
```

### `JobKind` / `JobStatus`

```python
class JobKind(str, Enum):
    SUBAGENT = "subagent"
    SHELL = "shell"

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})
```

### `JobsPort` consumer Protocol

```python
class JobsPort(Protocol):
    @property
    def closing(self) -> bool: ...
    async def create(
        self, *,
        kind: JobKind,
        metadata: dict[str, JsonValue] | None = None,
        parent_job_id: JobId | None = None,
        name: str | None = None,
    ) -> Job: ...
    def start(self, job_id: JobId, runner: JobRunner) -> Job: ...
    def get_or_none(self, job_id: JobId) -> Job | None: ...
    def all(self) -> list[Job]: ...
    def list(
        self, *,
        kind: JobKind | None = None,
        status: JobStatus | None = None,
        parent_job_id: JobId | None = None,
        recursive: bool = False,
        max_results: int = 20,
    ) -> list[JobSummary]: ...
    async def wait(
        self, ids: list[JobId], *,
        mode: WaitMode = "all",
        timeout: float | None = None,
    ) -> WaitResult: ...
    async def cancel(self, job_id: JobId) -> CancelResult: ...
```

### `JobRunner` Protocol

```python
class JobRunner(Protocol):
    async def run(self, job: Job, ctx: JobRunnerContext) -> JobResult: ...
    async def cancel(self, job: Job) -> None: ...
```

### `JobResult` / `JobError` / `JobSummary` / `WaitResult` / `CancelResult`

```python
@dataclass(slots=True)
class JobResult:
    summary: str | None = None
    output_store: TextOutputStorePort | None = None
    data: dict[str, JsonValue] = field(default_factory=dict)

class JobError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: str
    message: str
    detail: str | None = None

class JobSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: JobId
    kind: str
    status: str
    name: str | None = None
    elapsed_ms: int = 0
    parent_job_id: JobId | None = None
    summary: str | None = None

class WaitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ready: list[JobSummary] = Field(default_factory=list)
    pending: list[JobId] = Field(default_factory=list)
    timed_out: bool = False

class CancelResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: JobId
    status: str
    cancelled: bool = False
```

### `OutputStore` / `TextOutputStorePort` / `OutputChunk`

```python
@dataclass(frozen=True, slots=True)
class OutputChunk:
    data: str
    next_cursor: int | None = None
    eof: bool = False
    truncated: bool = False

class OutputStore(Protocol):
    async def read(
        self, *, cursor: int | None = None, max_bytes: int = 8000
    ) -> OutputChunk: ...

class TextOutputStorePort(OutputStore, Protocol):
    async def write(self, text: str) -> None: ...
    def all(self) -> str: ...
```

### `TaskCallback` / `TaskSnapshot` / `TaskCatalog`

```python
TaskCallback = Callable[[TaskSnapshot], Awaitable[None]]

@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    task_id: str = Field(min_length=1)
    kind: Literal["shell", "agent"] = "shell"
    command: str = ""
    cwd: str
    status: Literal["pending", "running", "completed", "failed", "stopped"]
    created_at: float = Field(ge=0)
    started_at: float = Field(ge=0)
    finished_at: float = Field(ge=0)
    output: str = ""
    error: str = ""
    agent: str = ""
    thread_id: str = ""
    usage: dict[str, JsonValue] = Field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class TaskCatalog:
    tasks: tuple[TaskSnapshot, ...]
```

Protocol kind mapping: `SUBAGENT → "agent"`, `SHELL → "shell"`.
Protocol status mapping: `CANCELLED → "stopped"`.

### `JobContext` / `_OutputFactory`

```python
class JobContext:
    def __init__(self) -> None:
        self.outputs = _OutputFactory()
        self.primary_output: TextOutputStorePort | None = None

class _OutputFactory:
    @staticmethod
    def create_text(text: str = "") -> TextOutputStore: ...
```

### `JobsComponent` / `JobHandlers`

```python
class JobsComponent:
    inject = {"required": ["commands", "engine"]}
    name = "xbot.jobs"

    def apply(self, ctx: Any, config: Any = None) -> None:
        max_concurrent = int((config or {}).get("max_concurrent_subagents", 4))
        registry = JobRegistry(limits={JobKind.SUBAGENT: max_concurrent})
        ctx.set("jobs", registry)
        for command in build_jobs_commands(registry):
            ctx.commands.register(command)
        handlers = JobHandlers(registry, ctx.engine, ctx)
        registry.on_update = handlers.publish_update
        registry.on_complete = handlers.publish_completion
        ctx.on(LIST_TASKS.name, handlers.list_tasks)
        ctx.on(STOP_TASK.name, handlers.stop_task)
        ctx.on(STOP_ALL_TASKS.name, handlers.stop_all)
        ctx.on(PREPARE_FORK, handlers.prepare_fork)
        ctx.on(Events.SESSION_CLOSE, handlers.close)
```

### `build_jobs_commands`

```python
def build_jobs_commands(jobs: JobsCommandPort) -> tuple[Command, ...]:
    # /tasks [ps] → list background tasks
    # /task stop <id> → stop one task
    # /task stopall → stop all tasks
```

### Events / Operations

```python
LIST_TASKS = Operation("jobs/list", EmptyRequest, TaskCatalog)
STOP_TASK = Operation("jobs/stop", StopTask, StoppedTasks)
STOP_ALL_TASKS = Operation("jobs/stop-all", EmptyRequest, StoppedTasks)

class StopTask:
    task_id: str

@dataclass(frozen=True, slots=True)
class StoppedTasks:
    tasks: tuple[TaskSnapshot, ...]
```

## Execution flow

```
JobRegistry._execute(job, runner) →
  acquire semaphore (if kind has limit) →
  job.status = RUNNING → notify_update() →
  runner.run(job, ctx) →
  on success: job.status = COMPLETED
  on CancelledError: job.status = CANCELLED
  on Exception: job.error = normalize_error(exc), job.status = FAILED
  finally: release semaphore, _finish(job, status) →
    notify_complete() → completion_event.set()
```

## How `apply()` works

```python
def apply(self, ctx, config=None):
    max_concurrent = int((config or {}).get("max_concurrent_subagents", 4))
    registry = JobRegistry(limits={JobKind.SUBAGENT: max_concurrent})
    ctx.set("jobs", registry)
    for command in build_jobs_commands(registry):
        ctx.commands.register(command)
    handlers = JobHandlers(registry, ctx.engine, ctx)
    registry.on_update = handlers.publish_update
    registry.on_complete = handlers.publish_completion
    # ... register operations
```

The registry is configured with a semaphore for `SUBAGENT` jobs
(default 4 concurrent). `on_update` publishes `TaskSnapshot` to
`RUNTIME_EVENT` (SSE). `on_complete` injects a prompt payload to
the engine and publishes `completion_notice` to the client.

## Cross-references

- Depends on: `commands`, `engine`, `agentloop` (`SESSION_CLOSE`),
  `session.contracts` (`PREPARE_FORK`).
- Depended on by: `subagents` (SUBAGENT runner), `coretools` (SHELL runner),
  `server-routes-jobs` (HTTP routes).
- Pairs with: `subagents` (domain adapter), `coretools` (domain adapter).

## Common pitfalls

- **`JobRegistry.create()` raises if closing**: raises `JobRegistryClosed`
  if `self._closing` is True. The session closing hook calls `shutdown()`
  which sets `_closing = True` before cancelling all tasks.
- **`JobRegistry.cancel()` on a terminal job returns `cancelled=False`**:
  cancellation only affects non-terminal jobs. Terminal jobs are
  left untouched.
- **`is_busy()` returns True if ANY job is pending/running**:
  used by `prepare_fork` to block forking while background tasks
  are active. `is_busy()` checks all jobs, not just SUBAGENT.
- **`JobKind.SHELL` has no concurrency limit**: only `SUBAGENT`
  is limited by the semaphore. SHELL jobs run unbounded.
- **`stop_all()` cancels and returns snapshots**: it cancels all
  non-terminal jobs, then returns `TaskSnapshot` for each. The
  snapshots reflect the cancelled status ("stopped").
- **`shutdown()` also drops all outputs**: after `stop_all()`,
  it calls `remove_all()` which clears job results. This is why
  output storage must be read before shutdown.
- **`JobRegistry.get_or_none()` returns None for unknown IDs**:
  unlike `get()` which raises `KeyError`. Use `get_or_none()` in
  tool handlers.
- **`list()` newest first**: `items.sort(key=lambda job: job.created_at, reverse=True)`.
- **`TaskSnapshot.kind` maps SUBAGENT → "agent"**: the protocol
  layer uses different kind names than the internal `JobKind`.
