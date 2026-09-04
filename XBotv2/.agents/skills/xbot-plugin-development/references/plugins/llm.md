# `llm`

Provider/model registry and selected `ModelPort`. Selects the active
provider+model at session start, surfaces a typed catalog for the UI,
and exposes operations for switching at runtime.

- **Import/profile:** `llm`, Agent and server profiles.
- **Source:** `XBotv2/llm/plugin.py`, `XBotv2/llm/services.py`,
  `XBotv2/llm/config.py`, `XBotv2/llm/commands.py`,
  `XBotv2/llm/runtime_commands/plugin.py`,
  `XBotv2/llm/anthropic.py`, `XBotv2/llm/openai.py`,
  `XBotv2/core/providers.py`, `XBotv2/core/usage.py`.
- **Injects/provides:** `runtime_log` → `llm` (`LLMService`) and
  `model` (`ModelService`).
- **Subscribes to events:** none in `apply`; the Agent loop drives
  `model.stream_message(...)` via `ctx.model`.

## Public data models

### `ProviderConfig` (`XBotv2/llm/config.py`)

```python
class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: str = "openai"               # "openai" | "anthropic" | "mock"
    base_url: str | None = None
    api_key: str | None = None             # may come from api_key_env
    default_model: str
    models: list[ModelConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_catalog(self) -> "ProviderConfig":
        # models non-empty + default_model is listed.
        ...

    def resolve(self, model: str | None = None) -> ModelConfig:
        name = model or self.default_model
        for candidate in self.models:
            if candidate.model == name:
                return candidate
        raise ValueError(f"Unknown model {name!r} for protocol ...")
```

### `ModelConfig`

```python
class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1)
    temperature: float | None = None
    max_context_tokens: int = Field(default=32_000, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    reasoning_effort: str | None = None
    effort: list[str] | None = None              # advertised effort tiers
    thinking: str | None = Field(default=None, min_length=1)
    extra_body: dict[str, Any] = Field(default_factory=dict)
    input_modalities: list[Literal["text", "image"]] = Field(
        default_factory=lambda: ["text"]
    )
    mock_responses: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def model_mode(self) -> str:
        return self.reasoning_effort or self.thinking or ""
```

Validation: `input_modalities` must contain `"text"`; if `effort` is
set, `reasoning_effort` must be one of its values.

### `parse_provider_config` / `expand_env`

```python
def parse_provider_config(
    raw: dict[str, Any],
    *,
    require_key: bool = True,
) -> ProviderConfig: ...

def expand_env(value: str) -> str: ...
    # ${VAR} or $VAR from os.environ; raises if unset.
```

`parse_provider_config` resolves `api_key_env` against the
environment and validates the catalog. `require_key=False` is the
listing path that leaves the key unresolved.

### `merge_request_extras`

```python
def merge_request_extras(
    derived: dict[str, Any],
    configured: dict[str, Any],
) -> dict[str, Any]: ...
    # Deep-merge with configured winning; used so a vendor can restate
    # or extend fields (e.g. Anthropic thinking + budget_tokens).
```

### `UsageData` (`XBotv2/core/usage.py`)

```python
class UsageData(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    requests: int = Field(default=0, ge=0)
    context_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    prompt_cache_write_tokens: int = Field(default=0, ge=0)
```

Preserve unknown provider values via the typed contract; do not
recalculate token counts.

### `ProviderCapabilities` (`XBotv2/core/providers.py`)

```python
@dataclass
class ProviderCapabilities:
    supported_input_modalities: frozenset[InputModality] = field(
        default=frozenset({"text"})
    )
    # ... other capability flags the adapter advertises
```

### `BaseProvider` (abstract base)

```python
class BaseProvider(ABC):
    supported_input_modalities: frozenset[InputModality] = frozenset({"text"})

    async def stream_message(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[ModelChunk]: ...

    def provider_schema(self) -> dict[str, Any]: ...     # JSON Schema fragment
```

Adapters in `XBotv2/llm/anthropic.py`, `XBotv2/llm/openai.py`.

## `LlmCatalogPort` / `ModelPort` Protocols

```python
class LlmCatalogPort(Protocol):
    def catalog(self) -> ProviderCatalog: ...

class ModelPort(Protocol):
    """Mutable model binding consumed by the Agent loop."""

    def bind_tools(
        self,
        tools: list[dict[str, JsonValue]],
        **kwargs: object,
    ) -> BaseProvider: ...

    def astream(
        self,
        messages: list[Message],
        **kwargs: object,
    ) -> AsyncIterator[ModelChunk]: ...
```

## Provider catalog model (`XBotv2/llm/contracts.py`)

```python
class ProviderCatalog:
    providers: list[ProviderInfo]           # name + protocol + base_url
    models: list[ModelInfo]                # model id + context window + capabilities
```

## Slash commands (`/llm`, `/provider`, `/model`, `/effort`, `/thinking`)

Registered by `XBotv2/llm/runtime_commands/plugin.py`. Each takes a
single argument and updates the active Agent runtime selection:

| Command | Argument | Effect |
|---|---|---|
| `/provider <name>` | provider name in catalog | switches `ctx.model` |
| `/model <id>` | model id within current provider | updates `ModelConfig` |
| `/effort <tier>` | one of `effort[]` advertised by model | updates `reasoning_effort` |
| `/thinking <mode>` | provider-defined mode | updates `thinking` |

Selection is session/runtime configuration, not a new provider
config file.

## Typical extension: a provider adapter

```python
from XBotv2.core.providers import BaseProvider
from XBotv2.core.messages import Message, ModelChunk

class MyProvider(BaseProvider):
    supported_input_modalities = frozenset({"text", "image"})

    async def stream_message(self, messages, **kwargs):
        # yield ModelChunk instances incrementally
        async for chunk in self._client.stream(messages, **kwargs):
            yield ModelChunk(
                delta=chunk.delta,
                usage=chunk.usage,
                finish_reason=chunk.finish_reason,
            )

    def provider_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {...}}
```

A consumer declares `inject = ["llm"]` to access the registry; agents
read `ctx.model` for the active binding.

## Cross-references

- Depends on: `runtime_log`.
- Depended on by: `agent-runtime` (binds selected provider to the
  loop), `usage` (records per-request deltas), `permissions` (model
  context for allow/ask decisions), `compact` (model selection for
  summarization), `subagents` (subagent model override).
- Pairs with: [llm-commands.md](llm-commands.md) (slash commands for
  runtime selection).

## Common pitfalls

- **Logging or persisting `api_key`**: keep credentials in environment
  variables (`api_key_env`) or the runtime's secure storage; the
  parsed `ProviderConfig` may carry the resolved key only in memory.
- **Reimplementing `ProviderConfig.resolve`**: use it; it fails closed
  on unknown model names instead of silently reusing another model's
  settings.
- **Mutating `ProviderConfig.models` at runtime**: validation is
  one-shot at parse; reload via `replace_rules` rather than list
  surgery.
- **Ignoring `reasoning_effort` validation**: if a model declares
  `effort` tiers, the configured `reasoning_effort` must be one of
  them; otherwise the request errors at model time.
- **Recalculating token counts in plugin code**: rely on
  `UsageData` from `ModelResponse.usage`; preserve unknown fields.
