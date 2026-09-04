# `server-routes-llm`

LLM provider selection HTTP routes — list providers and switch provider/effort.
Registered via `contribute_router()` as `xbot.http.llm`.

- **Import/profile:** `server-routes-llm`, server profile.
- **Source:** `XBotv2/llm/protocol.py`,
  `XBotv2/llm/http/plugin.py`.
- **Injects/provides:** none (uses `contribute_router`).
- **Subscribes to events:** `http/route` (`REGISTER_ROUTE`).

## Routes (`build_router`)

```python
def build_router(*, events: Any, sessions: SessionsPort) -> APIRouter:
```

### `GET /providers` → `ProviderCatalog`

```python
@router.get("/providers", operation_id="list_providers")
async def list_providers() -> ProviderCatalog:
    return await dispatch_operation(events, LIST_PROVIDERS, EmptyRequest())
```

`ProviderCatalog` (from `llm/contracts.py`):

```python
@dataclass(frozen=True, slots=True)
class ProviderCatalog:
    providers: list[ProviderInfo]
    models: list[ModelInfo]

@dataclass(frozen=True, slots=True)
class ProviderInfo:
    name: str
    protocol: str
    base_url: str | None

@dataclass(frozen=True, slots=True)
class ModelInfo:
    model: str
    context_window: int
    max_output_tokens: int | None
    effort: list[str] | None
    input_modalities: list[str]
    has_api_key: bool
```

### `PUT /sessions/{session_id}/threads/{thread_id}/provider` → `ProviderSelectionResponse`

```python
@router.put(
    "/sessions/{session_id}/threads/{thread_id}/provider",
    operation_id="select_provider",
)
async def select_provider(
    session_id: str,
    thread_id: str,
    payload: ProviderSelectionRequest,
) -> ProviderSelectionResponse:
```

Dispatches `SELECT_PROVIDER` with `SelectProvider(payload.name, payload.model)`.
Raises `HttpServerError("model_not_found" or "provider_not_found", status=404)`
if the model or provider is unknown.

### `PUT /sessions/{session_id}/threads/{thread_id}/effort` → `EffortSelectionResponse`

```python
@router.put(
    "/sessions/{session_id}/threads/{thread_id}/effort",
    operation_id="select_effort",
)
async def select_effort(
    session_id: str,
    thread_id: str,
    payload: EffortSelectionRequest,
) -> EffortSelectionResponse:
```

Dispatches `SELECT_EFFORT` with `SelectEffort(payload.effort)`.
Raises `HttpServerError("unsupported_effort", status=400)` if the
effort tier is not advertised by the current model.

## Wire models

```python
class ProviderSelectionRequest(WireModel):
    name: str = Field(min_length=1)
    model: str | None = Field(default=None, min_length=1)

class ProviderSelectionResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_mode: str = ""

class EffortSelectionRequest(WireModel):
    effort: str = Field(min_length=1)

class EffortSelectionResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: str = ""
    model_mode: str = ""
    available: list[str] = Field(default_factory=list)
```

## Cross-references

- Depends on: `server` (`contribute_router`), `llm` (`LIST_PROVIDERS`),
  `sessions` (`SessionsPort`).
- Depended on by: HTTP LLM clients, TUI provider views.
- Pairs with: `llm` (`LlmCatalogPort`, `ModelPort`), `agent-runtime`
  (`SELECT_PROVIDER`, `SELECT_EFFORT`).

## Common pitfalls

- **`SELECT_PROVIDER` dispatch requires the session to be active**:
  if the session has been closed, `SessionsPort.dispatch()` raises
  `OperationError("session_not_active")`.
- **`payload.model` is optional in `ProviderSelectionRequest`**:
  if None, the provider's default model is used. This is validated
  against the `ModelConfig` catalog at dispatch time.
- **Effort tier validation**: `SelectEffort` checks that the effort
  tier is in `ModelConfig.effort` (advertised tiers). Unknown tiers
  raise `ValueError` → `HttpServerError("unsupported_effort")`.
