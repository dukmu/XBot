# Compact Plugin

`builtin_plugins/compact` replaces an old, completed conversation prefix with
one model-generated summary. It uses only the public plugin and Hook API; core
owns history replacement and persistence.

## Behavior

- The model-visible `compact` tool requests compaction before the next model
  call. Its result is a structured `ToolResult`.
- Human `/compact` runs the same Hook-owned compaction immediately when the
  session is idle. During an active turn, the command waits for the first idle
  boundary and then runs without requiring another model turn; it never
  interrupts an in-flight model or Tool stream.
- Automatic compaction uses the latest provider-reported effective context
  tokens for the current history. It triggers at `trigger_ratio` of
  `max_context_tokens` after reserving `output_reservation` tokens. The
  independent `trigger_chars` threshold also protects providers that omit or
  under-report usage.
- The split normally preserves recent human-user boundaries. A long Goal
  iteration with no human input uses assistant boundaries while preserving
  the configured number of recent assistant/tool iterations.
- Automatic compaction may run again in the same long Goal turn after the
  compacted history grows past a threshold again.
- The auxiliary model receives no tools and must return summary text only.
- The summary becomes a system history message. The Engine runs the existing
  `PRE_COMPACT` and `POST_COMPACT` Hooks and appends a `history_checkpoint`
  record. Earlier raw records remain available, while resume starts replay at
  the latest checkpoint.
- The summary instruction explicitly requires preservation of human directives;
  the plugin does not append the same directives a second time after summarizing.
- A cancelled summary propagates cancellation. A failed manual request reports
  the error; failed automatic compaction logs the failure and continues the turn
  with the original history.
- Each completed compaction logs model-visible history characters before and
  after replacement, summary characters, removed message count, and the
  provider-reported input/output/total tokens from the summary call. `/compact`
  returns the same metrics, and plugin diagnostics retain the latest set for
  runtime inspection. These auxiliary-call tokens also remain part of session
  usage because they are real provider usage.

Provider context usage and the character threshold are independent signals
because cumulative token usage measures cost, not current context size. The
character count includes message content and tool-call names and arguments,
including the persisted summary envelope; it makes no tokenizer-accuracy claim.

## Configuration

| Key | Default | Meaning |
|---|---:|---|
| `automatic` | `true` | Enable threshold-triggered compaction. |
| `trigger_chars` | `80000` | Independent history-size safety threshold. |
| `output_reservation` | `4096` | Context tokens reserved for model output. |
| `trigger_ratio` | `0.8` | Fraction of the remaining input budget that triggers compaction. |
| `keep_recent_turns` | `4` | Recent input turns or Goal iterations preserved verbatim. |
| `summary_max_chars` | `8000` | Maximum persisted summary length. |

Configuration is validated before plugin import. Schema defaults remain
documentation; `CompactPlugin.on_load()` owns the runtime defaults.

## Boundaries

Compaction does not expose the provider client, Engine, state store, or message
file to the plugin. `HookContext.invoke_model()` supplies one unbound auxiliary
call, and the plugin returns the existing `BEFORE_CONTEXT` compaction result.
Auxiliary calls do not recursively run model Hooks or stream assistant deltas.

The agent Tool and human `/compact` command are separate registrations owned by
the same plugin. Only the Agent path enters Tool Hooks and permissions; it sets
the manual-request flag for the next safe `BEFORE_CONTEXT` boundary. The plugin
preapproves that Tool request at `BEFORE_TOOL_CALL`. The human command acquires
the session turn lock and immediately runs the same `BEFORE_CONTEXT`,
`PRE_COMPACT`, `POST_COMPACT`, and persistence path without starting a model
turn. If another turn owns the lock, the command runs as soon as that turn ends.
