# Token Budget And Fine-Grained Hooks

This document records the Phase 1-3 freeze analysis for token estimation,
usage accounting, budget control, and the next hook surface needed to support
them as plugins. It is intentionally a design checkpoint, not an implemented
runtime contract.

## Current State

XBotv2 currently has a provider-facing hook path:

- `AFTER_CONTEXT_BUILD` observes the final provider message list.
- `AFTER_TOOL_SCHEMA_BIND` observes the selected tools and request metadata.
- `BEFORE_MODEL_REQUEST` can replace messages/tools or short-circuit the
  provider call.
- `AFTER_MODEL_RESPONSE` observes the raw model response.

There is no dedicated token estimator, token statistics collector, or token
budget controller yet. Existing related mechanisms are narrower:

- `AgentConfig.max_context_tokens` is configured but not enforced.
- Provider `max_tokens` limits output size only.
- `CoreStateStore.message_count()` tracks message count, not token cost.
- `ContextBuilder` memoizes the stable system prefix string but does not expose
  per-source prompt metadata.
- The default tool-result cache hook truncates oversized tool output by
  character count and stores the full content under session artifacts.

## Freeze Risk

Without source-level token accounting, future compact/planning/skills plugins
can silently consume context budget with static prompt fragments, tool schemas,
or history growth. A budget plugin can only make coarse decisions from the
current final message list. That is enough to fail closed before a model call,
but not enough to explain or optimize the budget by source.

The Phase 1-3 core can freeze as a plugin-capable foundation, but token
budgeting should remain a documented Phase 4+ plugin target until these
extension points are added.

## Hook Stages Worth Adding

Add these in priority order when implementing token budget plugins:

| Stage | Purpose |
|-------|---------|
| `BEFORE_USER_MESSAGE_ACCEPT` | Validate or reject user input before it enters history. |
| `AFTER_USER_MESSAGE_ACCEPT` | Record the accepted user-message delta. |
| `BEFORE_CONTEXT_BUILD` | Let plugins prepare context-build parameters before assembly. |
| `AFTER_CONTEXT_COMPONENTS_BUILD` | Expose source-tagged context components before they become provider messages. |
| `BEFORE_TOOL_SCHEMA_BIND` | Let plugins filter visible tools before provider binding. |
| `ON_MODEL_REQUEST_ERROR` | Distinguish provider-call failures from generic engine errors. |
| `ON_TOOL_CALLS_PARSED` | Observe normalized tool calls before execution. |
| `BEFORE_TOOL_CALL` | Per-tool-call gate for permissions, auditing, and argument rewrites. |
| `AFTER_TOOL_CALL` | Per-tool-call result observation before batch-level `AFTER_TOOLS`. |
| `ON_TOOL_DENIED` | Structured event for sandbox or permission denial. |
| `BEFORE_STATE_PERSIST` | Snapshot plugin stats before materialization. |
| `AFTER_STATE_PERSIST` | Confirm persistence and emit bookkeeping events. |

Do not add per-fragment hooks first. Source-tagged context components are a
cleaner surface: they keep ordering deterministic and avoid turning prompt
rendering into a large hook matrix.

## Token Budget Plugin Shape

The recommended built-in plugin is `builtin_plugins/token_budget/`. It should
own all token-specific policy through its `PluginStore`; core should only
provide hooks and request metadata.

### TokenEstimator

Responsibilities:

- Estimate text, message, tool-schema, and complete request token costs.
- Prefer provider-specific tokenizers when available.
- Fall back to a conservative character-based estimate when no tokenizer is
  available.
- Record the estimator kind and confidence for every estimate.

### TokenStatsCollector

Responsibilities:

- Collect static overhead for system prompt, personality instructions, runtime
  rules, sandbox summary, and plugin prompt fragments.
- Collect dynamic overhead for history, current state, tool schemas, tool
  results, and model responses.
- Reconcile estimated prompt/completion tokens with provider usage metadata
  when the provider returns it.
- Persist latest request stats and append per-turn usage records in plugin
  state or session artifacts.

### TokenBudgetController

Responsibilities:

- Enforce `max_context_tokens` with explicit output-token reservation.
- Issue soft-limit events before hard failure.
- Coordinate with a compact plugin by returning a context replacement or
  compaction request from `BEFORE_MODEL_REQUEST`.
- Filter expensive tool schemas through `BEFORE_TOOL_SCHEMA_BIND` when needed.
- Short-circuit the provider call with a structured `token_budget_exceeded`
  event when no safe request can be built.

## Suggested Rollout

1. Add hook stages and `HookContext` fields for user acceptance, context
   components, tool calls, tool-call result metadata, and provider errors.
2. Extend `ContextBuilder` with an optional source-tagged component build path
   while keeping the existing `build()` API compatible.
3. Move tool filtering before provider `bind_tools()` so
   `BEFORE_TOOL_SCHEMA_BIND` can affect the actual bound client.
4. Implement `token_budget` in observe-only mode: estimator plus stats, no
   request rejection.
5. Enable soft/hard budget policy after stats are persisted and covered by
   tests.

## Evidence Required Before Freezing This Surface

- Unit tests prove every new hook receives the intended context fields.
- Engine tests prove tool filtering happens before provider binding.
- Engine tests prove provider errors trigger `ON_MODEL_REQUEST_ERROR` and still
  flow through `ON_ERROR`.
- Context tests prove component metadata preserves render order and plugin
  ownership.
- Token plugin tests prove estimate-only mode persists source breakdowns without
  changing runtime behavior.
- Budget plugin tests prove hard-limit short-circuit avoids provider calls and
  emits a bounded protocol event.
