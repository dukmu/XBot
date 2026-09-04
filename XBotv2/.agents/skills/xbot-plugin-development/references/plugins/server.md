# `server`

The HTTP/SSE FastAPI carrier — builds the application, registers middleware,
and exposes the `REGISTER_ROUTE` event for protocol plugins to contribute
routes. Business state and session lifecycle do not belong to the carrier.

- **Import/profile:** `server`, server profile.
- **Source:** `XBotv2/server/plugin.py`,
  `XBotv2/server/http.py`,
  `XBotv2/server/contracts.py`.
- **Injects/provides:** `runtime_log` → `server` (`FastAPI`),
  `server_info` (`ServerInfo`).
- **Subscribes to events:** `http/route` (`REGISTER_ROUTE`) — mounts
  route contributions with full cleanup.
- **Operations:** `QUERY_STATUS` (`server/status`).

## Public data models

### `ServerComponent`

```python
class ServerComponent:
    name = "xbot.server"
    inject = ["runtime_log"]

    def apply(self, ctx: Any, config: Any = None) -> None:
        from XBotv2.server.http import create_app
        info = ServerInfo(name="xbotv2", started_at=time.monotonic())
        app = create_app(server_name=info.name, runtime_log=ctx.runtime_log)
        carrier = WebServer(app)
        ctx.on(REGISTER_ROUTE, carrier.register_contribution)
        ctx.set("server_info", info)
        ctx.set("server", app)
```

### `WebServer` (`XBotv2/server/plugin.py:24-80`)

```python
class WebServer:
    def __init__(self, app: FastAPI) -> None: ...

    def register(self, router: APIRouter) -> Disposer:
        """Mount an APIRouter and return the disposer that unmounts it."""

    def register_contribution(
        self, contribution: RouteContribution
    ) -> Disposer:
        """Mount routes and exception handlers as one disposable effect."""
```

Route collision detection:

```python
def _route_keys(owner: FastAPI | APIRouter) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for route in getattr(owner, "routes", []):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        for method in methods:
            keys.add((method.upper(), str(path)))
    return keys
```

If `conflicts` is non-empty, raises `RuntimeError("web_server route
collision: ...")`.

### `create_app` (`XBotv2/server/http.py:130-170`)

```python
def create_app(
    *,
    server_name: str = "xbotv2",
    runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
) -> FastAPI:
    """Create an empty protocol carrier; plugins contribute every route."""
    error_responses = {
        status: {"model": ErrorResponse, "description": description}
        for status, description in {
            400: "Invalid request",
            404: "Resource not found",
            409: "Resource state conflict",
            410: "Interaction no longer pending",
            422: "Request schema validation failed",
            426: "Unsupported protocol version",
            500: "Server error",
        }.items()
    }
    app = FastAPI(
        title=server_name,
        version=PROTOCOL_VERSION,
        responses=error_responses,
    )
    app.add_middleware(ApiLoggingMiddleware, runtime_log=runtime_log)
    app.add_exception_handler(HttpServerError, _on_http_error)
    app.add_exception_handler(OperationError, _on_operation_error)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    return app
```

### Exception handler mapping (`OperationError → status code`)

| Condition | Status |
|---|---|
| `exc.code.endswith("_not_found")` | 404 |
| `exc.code in {"event_stream_connected", "parent_thread_not_active", "task_not_background", "thread_busy"}` | 409 |
| else | 400 |

### `ApiLoggingMiddleware`

```python
class ApiLoggingMiddleware:
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Logs request, injects x-request-id header, logs response
```

### `set_llm_override`

```python
def set_llm_override(app: FastAPI, llm: BaseProvider | None) -> None:
    """Override the model provider through FastAPI's dependency mechanism."""
    if llm is None:
        app.dependency_overrides.pop(current_model_override, None)
    else:
        app.dependency_overrides[current_model_override] = partial(
            _fixed_model, llm
        )
```

Used by tests and embedded server compositions.

### `RouteContribution` / `contribute_router` (`XBotv2/server/contracts.py`)

```python
@dataclass(frozen=True, slots=True)
class RouteContribution:
    owner: str
    router: APIRouter
    exception_handlers: tuple[
        tuple[type[Exception], ExceptionHandler], ...
    ] = ()

class RouteEventContext(Protocol):
    async def bail(self, event: str, *args: object) -> object: ...
    def dispose(self, callback: Disposer) -> Disposer: ...

async def contribute_router(
    ctx: RouteEventContext,
    *,
    owner: str,
    router: APIRouter,
    exception_handlers: tuple[
        tuple[type[Exception], ExceptionHandler], ...
    ] = (),
) -> None:
    """Register an adapter router and bind cleanup to its XCore fiber."""
```

## Server routes (core)

The `server` plugin does NOT register any routes directly. Route plugins
(`server-routes-core`, `server-routes-session`, etc.) use `contribute_router()`
to register their `APIRouter`:

```python
# server/routes/plugin.py
class CoreHttpPlugin:
    name = "xbot.http.core"
    inject = ["server", "server_info"]

    async def apply(self, ctx, config=None):
        await contribute_router(
            ctx,
            owner=self.name,
            router=build_core_router(events=ctx, info=ctx.server_info),
        )
```

## Cross-references

- Depends on: `runtime_log`.
- Depended on by: all `server-routes-*` plugins, `acp-plugin`.
- Pairs with: `server.routes.core`, `server.routes.session`, etc.
  (route plugins).

## Common pitfalls

- **Route collision detection**: the `WebServer.register()` checks
  for conflicting path+method pairs before mounting. A duplicate
  raises `RuntimeError` immediately — this catches composition
  errors at startup, not at request time.
- **Exception handler collision**: `register_contribution()` checks
  `self.app.exception_handlers` for duplicate handler types and
  raises if present.
- **`contribute_router()` requires `bail()`**: the event must be
  handled by the server plugin (which is guaranteed if the server
  plugin mounts first). If the server is not available, raises
  `RuntimeError("HTTP route carrier is unavailable")`.
- **`set_llm_override()` uses FastAPI's dependency override**: this
  only affects routes that declare `ModelOverride` as a dependency.
  Standard routes should not depend on this — it's a test hook.
