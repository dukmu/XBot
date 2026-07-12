# Token Manager Plugin

`builtin_plugins/token_manager` is the built-in template for observation and
policy plugins. It imports only from `xbotv2.api` and uses existing Hook fields;
it does not require token-specific core APIs.

## Current Behavior

The plugin currently operates in `observe_only` mode:

- estimates tokens for provider messages and visible tool schemas;
- records provider-reported input, output, and cache token usage;
- records context message counts and tool-call counts per turn;
- compares each model request with configured soft and hard thresholds;
- logs threshold violations without changing or rejecting the request;
- exposes in-memory usage through plugin diagnostics;
- resets plugin-owned memory during unload.

It does not compact messages, filter tools, reject provider calls, emit budget
events, or persist statistics. `max_context_tokens` is therefore a measurement
threshold, not an enforcement guarantee.

## Hook Usage

| Stage | Behavior |
|---|---|
| `ON_TURN_START` | Start one in-memory turn record. |
| `BEFORE_MODEL_REQUEST` | Estimate the current public `model_request` messages and tools, then check thresholds. |
| `AFTER_MODEL_RESPONSE` | Record provider usage metadata when present. |
| `ON_TOOL_CALLS_PARSED` | Count normalized tool calls. |
| `ON_TURN_END` | Finish the current turn record. |

These are existing `HookStage` values. Token manager evolution must reuse their
documented fields before proposing another public type or stage.

## Estimation And Statistics

The estimator currently uses a provider-neutral character approximation. Tool
schemas are included in the request estimate. This is useful for relative
observation, but it is not a tokenizer-accurate limit and can underestimate
languages or payloads with different tokenization. Estimates remain distinct
from provider-reported usage.

`TokenStatsCollector` keeps completed turns plus the active turn in memory. Its
summary includes cumulative prompt, completion, and cache tokens together with
the latest turn. Unload clears this state. It is not a durable accounting log.

## Configuration

The plugin accepts these validated values:

| Key | Default | Meaning |
|---|---:|---|
| `max_context_tokens` | `32000` | Total context threshold used by the checker. |
| `output_reservation` | `4096` | Tokens reserved before calculating the hard input threshold. |
| `soft_limit_ratio` | `0.8` | Fraction of the hard input threshold used for warnings. |

JSON Schema validation runs before plugin import. Runtime defaults remain
explicit in `TokenManagerPlugin.on_load`; schema defaults are not injected.

Diagnostics return:

```json
{
  "status": "ready",
  "mode": "observe_only",
  "usage": {}
}
```

## Remaining Behavior Gaps

Budget enforcement is not implemented. Before adding it, define and test:

1. whether a hard violation requests compaction, filters tools, or ends a turn;
2. the exact Hook return and C/S event observed by plugins and clients;
3. output-token reservation behavior for each provider;
4. provider/model-specific tokenizers and an explicit fallback confidence;
5. persistence and reconciliation of estimates with provider usage;
6. failure behavior when statistics cannot be persisted.

Do not treat API stability as a freeze gate. Update the API inventory only when
a repeated contract gap cannot be expressed with the current public fields, and
keep implementation, documentation, and behavior tests in the same change.
