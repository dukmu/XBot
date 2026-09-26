# `jobs`

Kind-agnostic background job lifecycle registry. It owns every lifecycle
concern shared by all job kinds: identity, status transitions, waiting,
cancellation, event notification, result storage, and cleanup.

Domain adapters (shell, subagent) implement `JobRunner` and their own
model-facing Tools; they never hold job state themselves.

- **Import/profile:** `jobs`, Agent profile.
- **Source:** `XBotv2/jobs/plugin.py`, `contracts.py`, `registry.py`,
  `protocol.py`, `commands.py`.
- **Injects/provides:** `commands`, `engine` → `jobs` (`JobRegistry`).
- **Operations:** `jobs/list` (`LIST_JOBS`, `EmptyRequest → JobCatalog`),
  `jobs/stop` (`STOP_JOB`, `StopJob → StoppedJobs`), `jobs/stop-all`
  (`STOP_ALL_JOBS`, `EmptyRequest → StoppedJobs`).
- **Events:** emits `job/updated` (`JOB_UPDATED`) and `job/completed`
  (`JOB_COMPLETED`), both carrying a `JobView`; also observes `session/close`
  and `prepare/fork`.
- **Commands:** `/jobs [ps]`, `/jobs stop <id>`, `/jobs stopall`.

There is no `jobs/runner.py` and no `jobs/output.py`. `JobRunner` is a Protocol
declared in `jobs/contracts.py`, and a job's result is the `result` field of the
`Succeeded` state — not a separate output subsystem.

## Composition

Job execution and its HTTP projection are one capability, mounted from a single
root export:

```python
class JobsRuntimeComponent:
    inject = {"required": ["commands", "engine"]}
    name = "xbot.jobs"
    Config = JobsConfig

    def apply(self, ctx: Context, config: JobsConfig) -> None: ...


class JobsPlugin:
    name = "xbot.jobs"
    Config = JobsConfig

    async def apply(self, ctx: Context, config: JobsConfig) -> None:
        await ctx.plugin(JobsRuntimeComponent(), config)
        await ctx.inject(["server", "sessions"], mount_http)


plugin = JobsPlugin()
```

`JobsRuntimeComponent` is the Agent-profile fiber and carries the `inject`
declaration. `JobsPlugin` declares no `inject` at all: its only job is to mount
the runtime component and gate the HTTP contribution on `server` + `sessions`.
Do not add a second tree entry for the HTTP facet.

## Configuration

```python
class JobsConfig(BaseModel):
    max_concurrent_subagents: int = Field(default=4, ge=1)
    model_config = ConfigDict(extra="forbid")
```

`max_concurrent_subagents` is the per-kind concurrency limit handed to
`JobRegistry(limits=...)`.

## State model

A job's status is a **closed union of seven frozen state dataclasses**, not an
enum and not a string field. `JobStateName` is the string projection used on the
wire.

```python
JobId = str
JobStateName = Literal[
    "queued", "running", "succeeded", "failed_before_start", "failed_running",
    "cancelled_before_start", "cancelled_running",
]
WaitMode = Literal["any", "all"]
MAX_SUMMARY_CHARS = 256
```

```python
@dataclass(frozen=True, slots=True)
class Queued:
    kind: Literal["queued"] = "queued"

@dataclass(frozen=True, slots=True)
class Running:
    started_at: float
    kind: Literal["running"] = "running"

@dataclass(frozen=True, slots=True)
class Succeeded:
    started_at: float
    finished_at: float
    result: object
    kind: Literal["succeeded"] = "succeeded"

@dataclass(frozen=True, slots=True)
class FailedBeforeStart:
    finished_at: float
    error: JobError
    kind: Literal["failed_before_start"] = "failed_before_start"

@dataclass(frozen=True, slots=True)
class FailedRunning:
    started_at: float
    finished_at: float
    error: JobError
    kind: Literal["failed_running"] = "failed_running"

@dataclass(frozen=True, slots=True)
class CancelledBeforeStart:
    finished_at: float
    reason: str
    kind: Literal["cancelled_before_start"] = "cancelled_before_start"

@dataclass(frozen=True, slots=True)
class CancelledRunning:
    started_at: float
    finished_at: float
    reason: str
    kind: Literal["cancelled_running"] = "cancelled_running"

JobState = (
    Queued | Running | Succeeded | FailedBeforeStart | FailedRunning
    | CancelledBeforeStart | CancelledRunning
)
```

The `before_start` / `running` split is deliberate: a cancelled or failed job
reports whether it ever began executing, so callers can distinguish "never ran"
from "ran and then died".

## Public data models (`jobs/contracts.py`)

```python
@dataclass(frozen=True, slots=True)
class JobIdentity:
    id: JobId
    owner: str
    parent: JobId | None
    name: str | None
    created_at: float


@dataclass(slots=True)          # not frozen: the state field is replaced in place
class Job:
    identity: JobIdentity
    spec: JobSpec
    state: JobState = field(default_factory=Queued)


@dataclass(frozen=True, slots=True)
class JobError:
    code: str
    message: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class WaitResult:
    ready: tuple[JobView, ...]
    pending: tuple[JobId, ...]
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class CancelResult:
    id: JobId
    status: JobStateName
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class JobCatalog:
    jobs: tuple[JobView, ...]


@dataclass(frozen=True, slots=True)
class StopJob:
    job_id: str


@dataclass(frozen=True, slots=True)
class StoppedJobs:
    jobs: tuple[JobView, ...]


@dataclass(frozen=True, slots=True)
class OutputPage:
    data: str
    next_cursor: int | None = None
    eof: bool = False
    truncated: bool = False
```

`Job` exposes derived read-only properties rather than duplicating
`identity`/`spec` fields: `id`, `kind`, `parent_job_id`, `name`, `created_at`,
`terminal`, `status` (the `JobStateName`), `started_at`, `finished_at`,
`result`, `error`, and `elapsed_ms`. Use those instead of pattern-matching on
`job.state` in callers. There is no `owner` property — read `job.identity.owner`.

`OutputPage` is public and exported but currently unreferenced inside the
package. It is not the job result type; treat `Succeeded.result` as the result.

## Wire model

```python
class JobView(BaseModel):
    id: JobId = Field(min_length=1)
    kind: str = Field(min_length=1)
    label: str = Field(min_length=1)
    state: JobStateName
    elapsed_ms: int = Field(ge=0)
    summary: str | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)
```

`JobView` is the only job shape that crosses a boundary — events, operations,
and HTTP all use it. `Job` itself is runtime-only mutable state and must not be
serialized.

## Protocols

```python
class JobSpec(Protocol):
    kind: str
    label: str


class JobRunner(Protocol):
    async def run(self, job: Job) -> object: ...
    async def cancel(self, job: Job) -> None: ...


class JobsPort(Protocol):
    @property
    def closing(self) -> bool: ...
    async def create(self, *, spec: JobSpec, owner: str,
                     parent_job_id: JobId | None = None,
                     name: str | None = None) -> Job: ...
    def start(self, job_id: JobId, runner: JobRunner) -> Job: ...
    def get_or_none(self, job_id: JobId) -> Job | None: ...
    def all(self) -> list[Job]: ...
    def list(self, *, kind: str | None = None,
             status: JobStateName | None = None,
             parent_job_id: JobId | None = None, recursive: bool = False,
             max_results: int = 20) -> list[JobView]: ...
    async def wait(self, ids: list[JobId], *, mode: WaitMode = "all",
                   timeout: float | None = None) -> WaitResult: ...
    async def cancel(self, job_id: JobId) -> CancelResult: ...


class JobsCommandPort(Protocol):
    def views(self) -> list[JobView]: ...
    def get_or_none(self, job_id: str) -> Job | None: ...
    async def cancel(self, job_id: str) -> CancelResult: ...
    async def stop_all(self) -> list[JobView]: ...


class JobEventPort(Protocol):
    async def emit(self, event: str, *args: object) -> None: ...
```

`JobRunner.run` returns the domain's own result object; the registry stores it
in `Succeeded.result` without interpreting it. `JobEventPort` is exported from
`jobs.contracts` but not re-exported from `jobs.__init__` — import it from
`XBotv2.jobs.contracts` when you need the publisher type.

## `JobRegistry` (`XBotv2/jobs/registry.py`)

```python
class JobRegistry(JobsPort):
    def __init__(
        self,
        *,
        limits: dict[str, int] | None = None,
        prefix: str = "job",
        publisher: JobEventPort | None = None,
    ) -> None: ...
```

All three parameters are keyword-only. `limits` maps a job `kind` to its
concurrency ceiling and is enforced with a per-kind semaphore; `prefix` seeds
generated ids.

Beyond the `JobsPort` methods, the concrete registry adds `views()`,
`is_busy()`, `stop_all()`, `shutdown()`, `remove(job_id)`, `remove_all()`, and
the `JobRegistry.view(job)` classmethod. These are not part of the port — a
plugin that only needs the lifecycle contract should inject `jobs` and stay on
`JobsPort`.

`normalize_error(exc)` converts an arbitrary exception into a `JobError` and is
the standard way a runner reports a failure.

## Events

```python
JOB_UPDATED = "job/updated"      # payload: JobView
JOB_COMPLETED = "job/completed"  # payload: JobView
```

The emitted argument is the `JobView` itself, not a wrapper. The wire models in
`jobs/protocol.py` are distinct from the bus payloads:

```python
class JobUpdatedEvent(WireModel):
    kind: Literal["job_updated"] = "job_updated"
    view: JobView


class JobCompletedEvent(WireModel):
    kind: Literal["job_completed"] = "job_completed"
    view: JobView
```

`job_updated_event(view)` and `job_completed_event(view)` are the constructors.
Completion is delivered to the Agent as an inbox item on the next step with
`wake=False`, so a finishing background job does not by itself create a new
turn.

## Commands

```python
Command(
    name="jobs",
    description="List or stop background jobs",
    handler=guard_command(jobs_command),
    effects=("jobs",),
    usage="/jobs [ps] | /jobs stop <id> | /jobs stopall",
    examples=("/jobs", "/jobs stop job-3", "/jobs stopall"),
)
```

`ps` is also the no-argument form. The listing line format is
`f"{job.kind}  {job.id}  {job.state}  {job.label}"`, and an empty registry
reports `"No background jobs."`.

## HTTP routes (`XBotv2/jobs/protocol.py`)

`def build_jobs_router(*, sessions: SessionsPort) -> APIRouter`, mounted
through `contribute_router(ctx, owner="xbot.jobs.http", ...)`.

| Method | Path | `operation_id` | Response |
|---|---|---|---|
| `GET` | `/sessions/{session_id}/threads/{thread_id}/jobs` | `list_jobs` | `JobListResponse` |
| `POST` | `/sessions/{session_id}/threads/{thread_id}/jobs/{job_id}/stop` | `stop_job` | `JobStopResponse` |
| `POST` | `/sessions/{session_id}/threads/{thread_id}/jobs/stop` | `stop_all_jobs` | `JobStopResponse` |

```python
class JobListResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    jobs: list[JobView] = Field(default_factory=list)


class JobStopResponse(JobListResponse):
    matched_count: int = Field(ge=0)
```

All three routes take path parameters only; the operation request objects are
constructed in the handler. Stopping an unknown job raises
`OperationError("job_not_found", ...)`, which the shared server error mapping
turns into a 404.

## Extension notes

- Implement `JobRunner` for a new background kind and register the job with
  `create(spec=..., owner=...)` / `start(...)`. Do not keep job state, ids, or
  waiting logic in the adapter.
- `prepare/fork` is refused with a retryable `thread_busy` `OperationError`
  while a background job is active. Preserve that: forking a thread with live
  jobs would duplicate them.
- Publish through `JobEventPort` rather than emitting bus events directly, so
  the registry stays the single source of id and ordering.
- Return a domain result from `run()` and let the registry store it; do not
  invent an output-store abstraction for a new job kind.
