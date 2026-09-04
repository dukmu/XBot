# `token-manager`

Observes the latest model request and response token usage without
owning context policy. Provides a diagnostic snapshot of context
token estimates per turn.

- **Import/profile:** `token-manager`, Agent profile.
- **Source:** `XBotv2/token_manager/plugin.py`.
- **Injects/provides:** `session` → (none directly; registers event
  listeners only).
- **Subscribes to events:** `model/request-ready` (context token estimate),
  `after/model-response` (provider usage data).
- **Diagnostics:** `ctx.token_manager.diagnostics()` returns the latest
  request/response token data.

## Public data models

### `TokenManagerPlugin` (`XBotv2/token_manager/plugin.py:12-60`)

```python
class TokenManagerPlugin:
    inject = ['session']
    name = "token_manager"

    def __init__(self) -> None:
        self._latest: dict[str, Any] = {}

    def apply(self, ctx, config=None) -> None:
        ctx.on(Events.MODEL_REQUEST_READY, self._on_model_request_ready)
        ctx.on(Events.AFTER_MODEL_RESPONSE, self._on_after_model_response)

    async def _on_model_request_ready(self, ctx: EventContext) -> None:
        """Compute context token estimate for the current request."""

    async def _on_after_model_response(self, ctx: EventContext) -> None:
        """Record provider usage metadata from the response."""

    def diagnostics(self) -> dict[str, Any]:
        """Return the latest request/response token snapshot."""
```

### `diagnostics()` return shape

```python
{
    "status": "ready",
    "mode": "observe_only",
    "latest_request": {
        "turn": 0,
        "message_count": 10,
        "tool_count": 3,
        "context_window": 32000,
        "context_tokens_estimate": 15000,
        "raw_estimate": 15000,
        "estimate_source": "calibrated",
        "utilization": 0.46875,
        "provider_usage": {
            "input_tokens": 1200,
            "output_tokens": 300,
            "total_tokens": 1500,
            "context_tokens": 1500,
            "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 0,
            "prompt_cache_write_tokens": 0,
        },
    },
}
```

### `calibrated_context_tokens` (`XBotv2/core/__init__.py`)

```python
def calibrated_context_tokens(
    messages: list[Message],
    tools: list[dict[str, Any]],
    full_messages: list[Message],
    provider: str,
    context_window: int,
) -> tuple[int, int, str]:
    """Return (context_tokens, raw_estimate, source)."""
```

Used in `_on_model_request_ready` to compute the context token estimate
for the current request.

## How `apply()` works

```python
def apply(self, ctx, config=None):
    ctx.on(Events.MODEL_REQUEST_READY, self._on_model_request_ready)
    ctx.on(Events.AFTER_MODEL_RESPONSE, self._on_after_model_response)
```

The plugin is **observe-only** — it does not modify context policy,
does not register tools or commands, and does not persist data.
It simply updates `self._latest` on each request/response cycle.

## Token estimation flow

```
MODEL_REQUEST_READY → calibrated_context_tokens(...) →
  _latest["context_tokens_estimate"] = estimate
  _latest["utilization"] = estimate / context_window

AFTER_MODEL_RESPONSE → ctx.model_response.usage_metadata →
  _latest["provider_usage"] = {key: int(value) for key in ...}
```

## Cross-references

- Depends on: `session`, `agentloop` (`MODEL_REQUEST_READY`,
  `AFTER_MODEL_RESPONSE`).
- Depended on by: diagnostics, monitoring, `usage` (reads `latest`
  for context token tracking).
- Pairs with: `usage` (cumulative provider usage), `compact`
  (context token budgeting).

## Common pitfalls

- **`diagnostics()` is the only public API**: the `_latest` dict is
  internal. Do not read `ctx.token_manager._latest` directly.
- **No persistence**: the plugin does not write to disk. For
  persistent token usage, use `usage` instead.
- **`context_tokens_estimate` is an estimate**: it uses
  `calibrated_context_tokens` which may differ from actual model
  token counts. The `estimate_source` field indicates the method.
- **`utilization` can be None**: if `context_window` is 0,
  `utilization` is `None` (division by zero guard).
- **Provider usage keys are optional**: not all providers report
  `cache_read_input_tokens`, `cache_creation_input_tokens`, etc.
  Missing keys are silently omitted from `provider_usage`.
