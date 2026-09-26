# `llm`

Owns provider configuration, provider/model catalog discovery, model binding,
and normalized provider stream handling. Provider-specific request, retry,
message, error, and usage behavior stays in the provider adapter.

- **Import/profile:** `XBotv2.llm`, Agent and server profiles.
- **Source:** `llm/contracts.py`, `config.py`, `service.py`, `plugin.py`,
  `openai.py`, and `anthropic.py`.
- **Configuration:** `LlmConfig` (`default_provider`, `providers`).
- **Operations:** `LIST_PROVIDERS`, `SELECT_PROVIDER`, `SELECT_EFFORT`.
- **Commands:** `/provider [status|list|use <name>]`, `/model [status|list|use
  [<provider>] <model>]`, `/effort [<level>]`.
- **HTTP:** provider catalog and per-thread provider/model/effort selection
  routes are documented in [LLM routes](server-routes-llm.md).

## Configuration contracts

```python
class LlmConfig(BaseModel):
    default_provider: str = "default"
    providers: dict[str, ProviderConfig] = {}

class ProviderConfig(BaseModel):
    protocol: str = "openai"
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    default_model: str
    models: list[ModelConfig] = []
    headers: dict[str, str] = {}

class ModelConfig(BaseModel):
    model: str
    temperature: float | None = None
    max_context_tokens: int = 32_000
    max_output_tokens: int | None = None
    reasoning_effort: str | None = None
    effort: list[str] | None = None
    thinking: str | None = None
    extra_body: dict[str, JsonValue] = {}
    input_modalities: list[Literal["text", "image"]] = ["text"]
```

Models must include text input. If effort tiers are declared,
`reasoning_effort`, when set, must be one of them. Provider configuration must
list at least one model and its `default_model`. API keys may be supplied
directly or resolved through `api_key_env`; do not log or persist resolved
credentials. `thinking` remains a provider-specific model configuration field;
the common generation selection exposes the current reasoning effort where the
model advertises it.

## Streaming contract

`ModelPort.astream(request: ModelRequest)` yields `ModelStreamEvent` values from
`XBotv2.core.stream`:

- `TextDelta(text)` for user-facing generated text;
- `ReasoningDelta(text)` for reasoning content, when emitted by the provider;
- `ToolCallDelta(call_id, name_delta, arguments_delta)` while a Tool call is
  being assembled;
- one terminal `ModelCompleted(response)`, `ModelFailed(error)`, or
  `ModelCancelled(reason)` event.

`ModelResponse` carries typed content parts, `UsageDelta`, observed context,
stop information, and provider extensions. The loop turns normalized text and
reasoning deltas into distinct client events; a Tool call is dispatched only
after its final arguments have been assembled and validated. Do not promise
reasoning deltas for every provider/model, and do not expose provider-native
chunks as a shared XBot contract.

The LLM plugin's model port accepts the typed `ModelRequest`; it does not accept
an arbitrary `messages, **kwargs` API. For a single auxiliary call, use
`invoke_llm(model, request)`, which consumes the same normalized event stream
and returns the terminal response or raises the provider failure.

## Catalog and selection

`ProviderCatalog` contains the configured default and provider descriptions;
each `ModelDescription` advertises model limits, reasoning effort, effort
tiers, thinking configuration, and input modalities. Runtime selection changes
the active thread's resolved runtime selection. Provider configuration itself
is startup input; no live config reload is promised.

The server command implementations are registered per active runtime. The
Textual TUI has local provider/model/effort handlers that use the public catalog
and selection API, and may present selectors when arguments are omitted. Other
clients should query the catalog rather than assume those TUI controls exist.

## Extension guidance

- Implement adapters behind `BaseProvider`/`ModelPort` and yield the normalized
  stream types.
- Keep raw provider errors, usage decoding, tool-call deltas, headers, and
  message format inside the provider implementation.
- Keep model limits and capabilities in validated config/catalog contracts.
- Usage accumulation is owned by [usage](usage.md), context-window recovery and
  compaction by [compact](compact.md), and client rendering by
  [client-runtime](../client-runtime.md).
