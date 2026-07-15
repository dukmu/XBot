# Compact Plugin

`builtin_plugins/compact` replaces an old, completed conversation prefix with
one model-generated summary. It uses only the public plugin and Hook API; core
owns history replacement and persistence.

## Behavior

- The model-visible `compact` tool requests compaction before the next model
  call. Its result is a structured `ToolResult`.
- Automatic compaction uses the latest provider-reported effective context
  tokens for the current history. It triggers at `trigger_ratio` of
  `max_context_tokens` after reserving `output_reservation` tokens. The
  independent `trigger_chars` threshold also protects providers that omit or
  under-report usage.
- The split normally occurs at a user or mailbox-system input boundary. A long
  Goal iteration with no later input uses assistant boundaries while preserving
  the configured number of recent assistant/tool iterations.
- Automatic compaction may run again in the same long Goal turn after the
  compacted history grows past a threshold again.
- The auxiliary model receives no tools and must return summary text only.
- The summary becomes a system history message. The Engine runs the existing
  `PRE_COMPACT` and `POST_COMPACT` Hooks and atomically rewrites messages during
  the normal persistence checkpoint.
- A failed or cancelled summary call returns no replacement. The original
  history remains intact and normal turn error behavior reports the failure.

Provider context usage and the character threshold are independent signals
because cumulative token usage measures cost, not current context size. The
character count includes message content and tool-call names and arguments; it
makes no tokenizer-accuracy claim.

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
the same plugin. Both set the plugin's manual-request flag, but only the Agent
path enters Tool Hooks and permissions. The plugin preapproves its Tool request
at `BEFORE_TOOL_CALL`. Compaction runs at the next normal `BEFORE_CONTEXT`
boundary.
