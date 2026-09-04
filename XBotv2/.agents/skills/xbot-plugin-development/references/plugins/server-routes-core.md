# `server-routes-core`

Core protocol routes — health check, hello, and server status.
Registered via `contribute_router()` as `xbot.http.core`.

- **Import/profile:** `server-routes-core`, server profile.
- **Source:** `XBotv2/server/protocol.py`.
- **Injects/provides:** none (uses `contribute_router`).
- **Subscribes to events:** `http/route` (`REGISTER_ROUTE`).

## Routes (`build_core_router`)

```python
def build_core_router(
    *,
    events: EventDispatcher,
    info: ServerInfo,
) -> APIRouter:
```

### `GET /health` → `HealthResponse`

```python
@router.get("/health", operation_id="health")
async def health() -> HealthResponse:
    status = await events.bail(QUERY_STATUS)
    # Queries SessionManager for sessions/threads count
    return HealthResponse(
        status="ok",
        server_name=info.name,
        uptime_s=int(time.monotonic() - info.started_at),
        sessions=status.sessions,
        threads=status.threads,
        workspace_root=status.workspace_root,
    )
```

If `QUERY_STATUS` returns non-`ServerStatus`, raises:
`OperationError("capability_unavailable", "server status capability is unavailable")`.

### `POST /hello` → `HelloResponse`

```python
@router.post("/hello", operation_id="hello")
async def hello(payload: HelloRequest) -> HelloResponse:
    if payload.protocol_version != PROTOCOL_VERSION:
        raise HttpServerError(
            "unsupported_protocol",
            f"Protocol {payload.protocol_version!r} is not supported; "
            f"expected {PROTOCOL_VERSION!r}",
            status=426,
        )
    return HelloResponse(
        server_name=info.name,
        session_id=(payload.session_id or "").strip(),
        thread_id=payload.thread_id.strip() or "agent",
    )
```

## Cross-references

- Depends on: `server` (`contribute_router`), `protocol` (`HealthResponse`, `HelloRequest`, `HelloResponse`).
- Depended on by: health check clients, protocol negotiators.
- Pairs with: `server` (route carrier), `process-sessions` (`QUERY_STATUS`).

## Common pitfalls

- **`/health` depends on `SessionManager`**: if no session plugin has
  mounted, `QUERY_STATUS` will fail. The health endpoint is only
  useful when the session carrier is active.
- **`/hello` returns 426 for wrong protocol version**: this uses
  `HttpServerError(status=426)` which maps to the `error_responses`
  dict in `create_app()`. The `ErrorResponse` model is the response schema.
