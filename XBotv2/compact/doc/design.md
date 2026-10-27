# Conversation compaction

Compact replaces a selected history prefix with a summary while retaining recent turns. The transaction and earlier trajectory remain durable; the service holds only transient selection and task state. It depends on the tool and command registries, model, loop state, usage, artifacts, and the injected context compiler. Summary requests reuse that compiler.

The plugin observes context-build, model-request-ready, model-request-error, model-response-observed and turn-end events. It emits before/after compact hooks and started/completed/failed runtime events. The `compact` Agent tool schedules compaction at the next context build; the human `/compact` command runs it immediately while idle. Neither path synthesizes a tool call. A provider-classified context overflow can trigger a bounded retry.

`CompactConfig` defaults: `automatic=True`, `output_reservation=None` (uses the active request's output budget), `trigger_ratio=0.8`, `keep_recent_turns=4`, and `summary_max_chars=8000`. The last value is an instruction to the summarizer, not a truncation limit. Values are resolved when the plugin is applied.

For example, `/compact` starts manual compaction while idle, and the following plugin configuration sets the number of recent turns retained at application time:

```yaml
- id: compact
  name: compact
  config:
    keep_recent_turns: 6
```

`CompactionSelection` records the expected history revision and source IDs; the commit checks them before replacing history. `CompactionPlan` and lifecycle events are public contracts in `api`; selection, summarization and execution live in `impl`.
