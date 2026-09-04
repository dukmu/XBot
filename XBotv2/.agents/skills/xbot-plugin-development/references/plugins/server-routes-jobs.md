# `server-routes-jobs`

Background task (job) HTTP routes — list tasks, stop one, stop all.
Registered via `contribute_router()` as `xbot.http.jobs`.

- **Import/profile:** `server-routes-jobs`, server profile.
- **Source:** `XBotv2/jobs/http/plugin.py` (registration),
  `XBotv2/jobs/protocol.py` (routes),
  `XBotv2/jobs/contracts.py` (data models).
- **Injects/provides:** none (uses `contribute_router`).
- **Subscribes to events:** `http/route` (`REGISTER_ROUTE`).

## Router registration (`XBotv2/jobs/http/plugin.py`)

```python
class JobsHttpPlugin:
    name = "xbot.http.jobs"
    inject = ["server", "sessions"]

    async def apply(self, ctx, config=None):
        await contribute_router(
            ctx,
            owner=self.name,
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

Routes are scoped per-session/thread: `/sessions/{session_id}/threads/{thread_id}/tasks/...`.

### `GET /sessions/{session_id}/threads/{thread_id}/tasks` → `TaskListResponse`

```python
@router.get(
    "/sessions/{session_id}/threads/{thread_id}/tasks",
    operation_id="list_tasks",
)
async def list_tasks_endpoint(
    session_id: str,
    thread_id: str,
) -> TaskListResponse:
```

Dispatches `LIST_TASKS` to `sessions.dispatch()`.

```python
class TaskListResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    tasks: list[TaskSnapshot] = Field(default_factory=list)
```

### `POST /sessions/{session_id}/threads/{thread_id}/tasks/{task_id}/stop` → `TaskStopResponse`

```python
@router.post(
    "/sessions/{session_id}/threads/{thread_id}/tasks/{task_id}/stop",
    operation_id="stop_task",
)
async def stop_task_endpoint(
    session_id: str,
    thread_id: str,
    task_id: str,
) -> TaskStopResponse:
```

Dispatches `STOP_TASK` to `sessions.dispatch()`.

```python
class TaskStopResponse(TaskListResponse):
    matched_count: int = Field(ge=0)
```

### `POST /sessions/{session_id}/threads/{thread_id}/tasks/stop` → `TaskStopResponse`

```python
@router.post(
    "/sessions/{session_id}/threads/{thread_id}/tasks/stop",
    operation_id="stop_all_tasks",
)
async def stop_all_tasks_endpoint(
    session_id: str,
    thread_id: str,
) -> TaskStopResponse:
```

Dispatches `STOP_ALL_TASKS` to `sessions.dispatch()`.

## Event helpers

```python
def task_updated_event(task: TaskSnapshot) -> ClientEvent:
    """Emit SSE 'task_updated' event."""

def task_completion_event(task: TaskSnapshot) -> ClientEvent:
    """Emit SSE 'completion_notice' event with TaskCompletionData payload."""
```

## Cross-references

- Depends on: `server` (`contribute_router`), `sessions` (`SessionsPort`).
- Depended on by: HTTP task clients, TUI task views.
- Pairs with: `jobs` (`JobRegistry`), `process-sessions` (dispatches tasks).

## Common pitfalls

- **Task IDs are strings**: `task_id` in the path is a `str` (matches `JobId = str`).
- **Routes are per-session/thread**: all paths require `session_id` and
  `thread_id` as path segments; the registry lives inside the session.
- **`stop_all_tasks` dispatches `STOP_ALL_TASKS`**: cancels every active
  task and returns their final snapshots.
