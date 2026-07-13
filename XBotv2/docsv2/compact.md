# Compact Plugin

`builtin_plugins/compact` replaces an old, completed conversation prefix with
one model-generated summary. It uses only the public plugin and Hook API; core
owns history replacement and persistence.

## Behavior

- The model-visible `compact` tool requests compaction before the next model
  call. Its result is a structured `ToolResult`.
- Automatic compaction runs when persisted history reaches `trigger_chars`.
- The split occurs at a user-message boundary, preserving the configured number
  of recent complete turns together with their tool calls and results.
- The auxiliary model receives no tools and must return summary text only.
- The summary becomes a system history message. The Engine runs the existing
  `PRE_COMPACT` and `POST_COMPACT` Hooks and atomically rewrites messages during
  the normal persistence checkpoint.
- A failed or cancelled summary call returns no replacement. The original
  history remains intact and normal turn error behavior reports the failure.

The trigger is intentionally a character count, not a tokenizer-accurate token
claim. It counts message content and tool-call names and arguments. A later
token-budget integration should replace this only when one shared estimator has
a documented provider-neutral contract.

## Configuration

| Key | Default | Meaning |
|---|---:|---|
| `automatic` | `true` | Enable threshold-triggered compaction. |
| `trigger_chars` | `80000` | History character threshold. |
| `keep_recent_turns` | `4` | Complete recent user turns preserved verbatim. |
| `summary_max_chars` | `8000` | Maximum persisted summary length. |

Configuration is validated before plugin import. Schema defaults remain
documentation; `CompactPlugin.on_load()` owns the runtime defaults.

## Boundaries

Compaction does not expose the provider client, Engine, state store, or message
file to the plugin. `HookContext.invoke_model()` supplies one unbound auxiliary
call, and the plugin returns the existing `BEFORE_CONTEXT` compaction result.
Auxiliary calls do not recursively run model Hooks or stream assistant deltas.

The tool can be invoked by the agent when the user requests compaction. Tool
entries shown by command discovery remain descriptive; the current generic
tool-command endpoint does not execute arbitrary tools directly.
