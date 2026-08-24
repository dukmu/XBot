# Compact Plugin

`compact` replaces an old, completed conversation prefix with
one model-generated summary. The plugin owns history replacement and publishes
the resulting core-state mutation for persistence observers.

## Behavior

- The model-visible `compact` tool requests compaction before the next model
  call. Its result is a structured `ToolResult`.
- Human `/compact` runs the same Hook-owned compaction immediately when the
  session is idle. During an active turn, the command waits for the first idle
  boundary and then runs without requiring another model turn; it never
  interrupts an in-flight model or Tool stream.
- Automatic compaction runs after the complete provider context has been built,
  oversized content has been externalized, and the visible Tool set is known.
  The latest provider-reported context size calibrates the current estimate, so
  the unchanged system prefix and Tool schemas are not repeatedly guessed.
- The trigger is the smaller of `trigger_ratio * max_context_tokens` and
  `max_context_tokens - output_reservation`. When the plugin setting omits
  `output_reservation`, the active provider's configured `max_output_tokens` is
  used; providers without an output cap rely on the ratio.
- The split normally preserves recent human-user boundaries. A long Goal
  iteration with no human input uses assistant boundaries while preserving
  the configured number of recent assistant/tool iterations.
- Automatic compaction may run again in the same long Goal turn after the
  compacted history grows past a threshold again.
- The auxiliary model receives no tools and must return summary text only. Its
  request starts with the same stable system context as a normal model request,
  allowing provider prefix caches to be reused. The summary instruction retains
  requirements, decisions, feedback, verified results, current state, remaining
  work, and known unknowns while distinguishing evidence from plans.
- The summary becomes a system history message. The plugin runs
  `PRE_COMPACT` and `POST_COMPACT`, then publishes the typed session
  `HISTORY_CHANGED` event. It commits exactly one atomic
  `ConversationHistory.replace()`; the persisted current history no longer
  retains or replays the removed raw prefix.
- The summary instruction explicitly requires preservation of human directives;
  the plugin does not append the same directives a second time after summarizing.
- A cancelled summary propagates cancellation. A failed manual request reports
  the error; failed automatic compaction logs the failure and continues the turn
  with the original history.
- Each completed compaction logs estimated context tokens before and after,
  estimate source, threshold, history characters, summary characters, removed
  message count, and provider usage from the summary call. Auxiliary calls
  remain part of cumulative session usage, but do not replace the latest
  main-Agent `context_tokens`.

`context_tokens` means the input size of the latest main-Agent provider request.
Session input/output/total fields are cumulative. Character counts are
diagnostic only and never trigger compaction.

## Configuration

| Key | Default | Meaning |
|---|---:|---|
| `automatic` | `true` | Enable threshold-triggered compaction. |
| `output_reservation` | provider output cap | Optional override for context tokens reserved for model output. |
| `trigger_ratio` | `0.8` | Fraction of the full context window that triggers compaction. |
| `keep_recent_turns` | `4` | Recent input turns or Goal iterations preserved verbatim. |
| `summary_max_chars` | `8000` | Maximum persisted summary length. |

Configuration is validated before plugin import. Schema defaults remain
documentation; `CompactPlugin.apply()` receives the validated runtime values.
The former `trigger_chars` setting was removed because a fixed character count
cannot represent 32K, 200K, and 1M provider windows consistently.

## Boundaries

Compaction captures its injected `ctx.llm` service when mounted; event payloads
do not expose the application container or an Engine callback. The plugin
commits the replacement and returns only a generic request-rebuild decision.
Auxiliary calls do not recursively run model Hooks or stream assistant deltas.

The agent Tool and human `/compact` command are separate registrations owned by
the same plugin. Only the Agent path enters Tool Hooks and permissions; it sets
the manual-request flag for the next safe `BEFORE_CONTEXT` boundary. The plugin
does not bypass the standard Tool guard pipeline. The human command acquires the
session turn lock and immediately runs the same `BEFORE_CONTEXT`, `PRE_COMPACT`,
and `POST_COMPACT` path without starting a model turn. If
another turn owns the lock, the command runs as soon as that turn ends.
Automatic requests are evaluated at `BEFORE_MODEL_REQUEST`; a successful replacement
causes Core to rebuild context before issuing any provider request.
