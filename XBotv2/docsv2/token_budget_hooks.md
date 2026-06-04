# Token Budget And Fine-Grained Hooks

This document records the Phase 1-3 freeze analysis for token estimation,
usage accounting, budget control, and the hook surface that supports them as
plugins. The hook surface described here is implemented in core; the token
estimator/statistics/budget plugin itself is still future work.

## Current State

XBotv2 currently has a provider-facing hook path:

- `BEFORE_USER_MESSAGE_ACCEPT` and `AFTER_USER_MESSAGE_ACCEPT` bracket user
  input admission into history.
- `BEFORE_CONTEXT_BUILD` can prepare context-builder inputs.
- `AFTER_CONTEXT_COMPONENTS_BUILD` observes source-tagged context components.
- `AFTER_CONTEXT_BUILD` observes the final provider message list.
- `BEFORE_TOOL_SCHEMA_BIND` can filter tools before provider binding.
- `AFTER_TOOL_SCHEMA_BIND` observes the selected tools and request metadata.
- `BEFORE_MODEL_REQUEST` can replace messages/tools or short-circuit the
  provider call.
- `AFTER_MODEL_RESPONSE` observes the raw model response.
- `ON_MODEL_REQUEST_ERROR` observes provider request failures.
- `ON_TOOL_CALLS_PARSED`, `BEFORE_TOOL_CALL`, `AFTER_TOOL_CALL`, and
  `ON_TOOL_CALL_FAILURE` expose tool-call lifecycle metadata.
- `ON_PERMISSION_REQUEST`, `ON_PERMISSION_DENIED`, `ON_TOOL_DENIED`, and
  `POST_TOOL_BATCH` expose policy and batch execution metadata.
- `ON_CLIENT_EVENT` exposes interaction traffic before events are persisted
  and streamed to clients.
- `PRE_COMPACT` and `POST_COMPACT` bracket message-history replacement.
- `ON_STOP` and `ON_STOP_FAILURE` expose final turn outcome and failure
  reasons.
- `BEFORE_STATE_PERSIST` and `AFTER_STATE_PERSIST` bracket message persistence
  and state materialization.

Pre-context and pre-request guard hooks must return structured dicts for
rewrites or custom stop events. A bare truthy short-circuit is converted to a
bounded `hook_short_circuit_rejected` error so a budget plugin cannot
accidentally let a provider request continue after it intended to stop.

There is no dedicated token estimator, token statistics collector, or token
budget controller yet. Existing related mechanisms are narrower:

- `AgentConfig.max_context_tokens` is configured but not enforced.
- Provider `max_tokens` limits output size only.
- `CoreStateStore.message_count()` tracks message count, not token cost.
- `ContextBuilder` memoizes the stable system prefix string and exposes
  source-tagged context components, but no token counter consumes them yet.
- The default tool-result cache hook truncates oversized tool output by
  character count and stores the full content under session artifacts.

## Freeze Risk

Without a token-budget plugin, future compact/planning/skills plugins can still
silently consume context budget with static prompt fragments, tool schemas, or
history growth. The implemented hook surface now provides the raw evidence
needed to explain and optimize budget by source, but no runtime module consumes
that evidence yet.

The Phase 1-3 core can freeze as a plugin-capable foundation, but token
budgeting should remain a documented Phase 4+ plugin target until estimator,
statistics, and policy modules are added.

## Implemented Hook Stages

These stages are now available for token budget plugins:

| Stage | Purpose |
|-------|---------|
| `BEFORE_USER_MESSAGE_ACCEPT` | Validate or reject user input before it enters history; silent rejection becomes a bounded error. |
| `AFTER_USER_MESSAGE_ACCEPT` | Record the accepted user-message delta. |
| `BEFORE_CONTEXT_BUILD` | Let plugins prepare context-build parameters before assembly. |
| `PRE_COMPACT` | Observe or rewrite a pending compaction before history replacement. |
| `POST_COMPACT` | Record message-count deltas after history replacement. |
| `AFTER_CONTEXT_COMPONENTS_BUILD` | Expose source-tagged context components before they become provider messages. |
| `BEFORE_TOOL_SCHEMA_BIND` | Let plugins filter visible tools before provider binding. |
| `ON_MODEL_REQUEST_ERROR` | Distinguish provider-call failures from generic engine errors. |
| `ON_TOOL_CALLS_PARSED` | Observe normalized tool calls before execution. |
| `ON_PERMISSION_REQUEST` | Observe sandbox or permission ask decisions before they fail closed. |
| `ON_PERMISSION_DENIED` | Observe sandbox or permission denials. |
| `BEFORE_TOOL_CALL` | Per-tool-call gate for permissions, auditing, and argument rewrites; rewritten ids and sandboxed paths are honored. |
| `AFTER_TOOL_CALL` | Per-tool-call result observation before batch-level `AFTER_TOOLS`. |
| `ON_TOOL_CALL_FAILURE` | Observe tool exceptions with the generated error ToolMessage. |
| `POST_TOOL_BATCH` | Observe all tool calls and results in one batch after per-call rewrites. |
| `ON_TOOL_DENIED` | Structured event for sandbox or permission denial. |
| `ON_CLIENT_EVENT` | Observe client-directed events such as `client_message`, `user_input_required`, and permission notices. |
| `ON_STOP` | Record successful turn stop reason. |
| `ON_STOP_FAILURE` | Record turn or stop failure reason. |
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

## Remaining Rollout

1. Implement `token_budget` in observe-only mode: estimator plus stats, no
   request rejection.
2. Enable soft/hard budget policy after stats are persisted and covered by
   tests.

## Evidence Required Before Freezing This Surface

- Core tests prove every new hook receives the intended context fields.
- Engine tests prove tool filtering happens before provider binding.
- Engine tests prove provider errors trigger `ON_MODEL_REQUEST_ERROR` and still
  flow through `ON_ERROR`.
- Context/engine tests prove component metadata preserves render order and
  plugin ownership.
- Token plugin tests prove estimate-only mode persists source breakdowns without
  changing runtime behavior.
- Budget plugin tests prove hard-limit short-circuit avoids provider calls and
  emits a bounded protocol event.
