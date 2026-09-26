# `server-routes-jobs`

Background job HTTP routes — list jobs, stop one, stop all. Registered via
`contribute_router()` as `xbot.jobs.http`.

- **Import/profile:** `server-routes-jobs`, server profile.
- **Source:** `XBotv2/jobs/plugin.py` (`mount_http`),
  `XBotv2/jobs/protocol.py` (routes),
  `XBotv2/jobs/contracts.py` (operations and data models).
- **Injects/provides:** none (uses `contribute_router`).
- **Registration:** the owning root plugin waits for `server` and `sessions`,
  then contributes the router as one fiber-owned server effect.

## Router registration (`XBotv2/jobs/plugin.py`)

```python
async def mount_http(ctx: Context) -> None:
    await contribute_router(
        ctx,
        owner="xbot.jobs.http",
        router=build_jobs_router(sessions=ctx.sessions),
    )
```

## Routes (`XBotv2/jobs/protocol.py`)

```python
def build_jobs_router(*, sessions: SessionsPort) -> APIRouter:
```

Routes are scoped per session/thread:
`/sessions/{session_id}/threads/{thread_id}/jobs/...`. All three take path
parameters only — the operation request objects are constructed in the handler,
and no route declares `response_model=` or an explicit `status_code`, so FastAPI
defaults (200) apply.

| Method | Path | `operation_id` | Response |
|---|---|---|---|
| `GET` | `/sessions/{session_id}/threads/{thread_id}/jobs` | `list_jobs` | `JobListResponse` |
| `POST` | `/sessions/{session_id}/threads/{thread_id}/jobs/{job_id}/stop` | `stop_job` | `JobStopResponse` |
| `POST` | `/sessions/{session_id}/threads/{thread_id}/jobs/stop` | `stop_all_jobs` | `JobStopResponse` |

### `GET .../jobs` → `JobListResponse`

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

Dispatches `LIST_JOBS` with `EmptyRequest()` through `sessions.dispatch()`.

### `POST .../jobs/{job_id}/stop` → `JobStopResponse`

Dispatches `STOP_JOB` with `StopJob(job_id)`. The response reports
`matched_count=1`. An unknown id raises
`OperationError("job_not_found", ...)`, which the shared server error mapping
(`XBotv2/server/http.py`) turns into a 404 because the code ends with
`_not_found`.

### `POST .../jobs/stop` → `JobStopResponse`

```python
@router.post(
    "/sessions/{session_id}/threads/{thread_id}/jobs/stop",
    operation_id="stop_all_jobs",
)
async def stop_all_jobs_endpoint(
    session_id: str,
    thread_id: str,
) -> JobStopResponse:
```

Dispatches `STOP_ALL_JOBS` with `EmptyRequest()`, and reports
`matched_count=len(result.jobs)`.

## Response models

```python
class JobListResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    jobs: list[JobView] = Field(default_factory=list)


class JobStopResponse(JobListResponse):
    matched_count: int = Field(ge=0)
```

The list field is `jobs`, and each entry is a `JobView`.

## SSE event models

The router declares the wire the client receives, but the events are emitted by
`JobHandlers` in `XBotv2/jobs/plugin.py`, not by the route handlers:

```python
class JobUpdatedEvent(WireModel):
    kind: Literal["job_updated"] = "job_updated"
    view: JobView


class JobCompletedEvent(WireModel):
    kind: Literal["job_completed"] = "job_completed"
    view: JobView


def job_updated_event(view: JobView) -> JobUpdatedEvent: ...
def job_completed_event(view: JobView) -> JobCompletedEvent: ...
```

`publish_update` emits `RuntimeEvent(event=job_updated_event(view))` on the
`runtime/event` bus event. `publish_completion` additionally submits an
`InboxItem` addressed to `InboxTarget.NEXT_STEP` whose payload is the
completion event serialized as JSON, with `wake=False` — so a finishing job
surfaces its result on the next step instead of forcing a new turn.

## Cross-references

- Depends on: `server` (`contribute_router`), `sessions` (`SessionsPort`).
- Depended on by: HTTP job clients and TUI job views.
- Pairs with: `jobs` (`JobRegistry`), `process-sessions` (dispatches jobs).

## Pitfalls

- `job_id` in the path is a `str`, matching `JobId = str`.
- Every path requires both `session_id` and `thread_id`; the registry lives
  inside the session runtime.
- Do not add request body models. Inputs are path-only, and the operation
  request objects are built in the handler.
- Do not rename `stop_all_jobs` to a task-flavoured name: the `operation_id`
  is part of the published client surface, and clients generate their calls
  from it.
