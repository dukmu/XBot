# `server-routes-jobs`

Background job (job) HTTP routes — list tasks, stop one, stop all.
Registered via `contribute_router()` as `xbot.http.jobs`.

- **Import/profile:** `server-routes-jobs`, server profile.
- **Source:** `XBotv2/jobs/plugin.py` (`mount_http`),
  `XBotv2/jobs/protocol.py` (routes),
  `XBotv2/jobs/contracts.py` (data models).
- **Injects/provides:** none (uses `contribute_router`).
- **Registration:** the owning root plugin waits for `server` and `sessions`,
  then contributes the router as one fiber-owned server effect.

## Router registration (`XBotv2/jobs/plugin.py`)

```python
async def mount_http(ctx: Context) -> None:
    await contribute_router(
        ctx,
        owner="xbot.jobs.http",
        router=build_tasks_router(sessions=ctx.sessions),
    )
```

## Routes (`build_tasks_router`) — `XBotv2/jobs/protocol.py`

```python
def build_tasks_router(
    *,
    sessions: SessionsPort,
) -> APIRouter:
```

Routes are scoped per-session/thread: `/sessions/{session_id}/threads/{thread_id}/jobs/...`.

### `GET /sessions/{session_id}/threads/{thread_id}/jobs` → `JobListResponse`

```python
@router.get(
    "/sessions/{session_id}/threads/{thread_id}/jobs",
    operation_id="list_jobs",
)
async def list_jobs_endpoint(
    session_id: str,
    thread_id: str,
) -> JobListResponse:
```

Dispatches `LIST_JOBS` to `sessions.dispatch()`.

```python
class JobListResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    tasks: list[JobSnapshot] = Field(default_factory=list)
```

### `POST /sessions/{session_id}/threads/{thread_id}/jobs/{job_id}/stop` → `JobStopResponse`

```python
@router.post(
    "/sessions/{session_id}/threads/{thread_id}/jobs/{job_id}/stop",
    operation_id="stop_job",
)
async def stop_job_endpoint(
    session_id: str,
    thread_id: str,
    job_id: str,
) -> JobStopResponse:
```

Dispatches `STOP_JOB` to `sessions.dispatch()`.

```python
class JobStopResponse(JobListResponse):
    matched_count: int = Field(ge=0)
```

### `POST /sessions/{session_id}/threads/{thread_id}/jobs/stop` → `JobStopResponse`

```python
@router.post(
    "/sessions/{session_id}/threads/{thread_id}/jobs/stop",
    operation_id="stop_all_tasks",
)
async def stop_all_tasks_endpoint(
    session_id: str,
    thread_id: str,
) -> JobStopResponse:
```

Dispatches `STOP_ALL_JOBS` to `sessions.dispatch()`.

## Event helpers

```python
def job_updated_event(task: JobSnapshot) -> ClientEvent:
    """Emit SSE 'job_updated' event."""

def job_completion_event(task: JobSnapshot) -> ClientEvent:
    """Emit SSE 'completion_notice' event with TaskCompletionData payload."""
```

## Cross-references

- Depends on: `server` (`contribute_router`), `sessions` (`SessionsPort`).
- Depended on by: HTTP task clients, TUI task views.
- Pairs with: `jobs` (`JobRegistry`), `process-sessions` (dispatches tasks).

## Common pitfalls

- **Task IDs are strings**: `job_id` in the path is a `str` (matches `JobId = str`).
- **Routes are per-session/thread**: all paths require `session_id` and
  `thread_id` as path segments; the registry lives inside the session.
- **`stop_all_tasks` dispatches `STOP_ALL_JOBS`**: cancels every active
  task and returns their final snapshots.
